import folium
import json
import polyline
import requests
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from flask import Flask, render_template, request, jsonify, send_from_directory
import heapq
import math
import matplotlib
matplotlib.use('Agg')  # Backend cho server
import matplotlib.pyplot as plt
import io
import base64
from matplotlib import font_manager
import numpy as np
from pymongo import MongoClient
import time
import traceback

# --- THƯ VIỆN TỐI ƯU ---
from scipy.spatial import cKDTree
from folium.plugins import MarkerCluster
# -----------------------

app = Flask(__name__)

# --- 1. LOAD DỮ LIỆU TỪ MONGODB & CACHE ---
print("Đang tải dữ liệu trạm sạc từ MongoDB...")
try:
    client = MongoClient('mongodb://localhost:27017/', maxPoolSize=1, serverSelectionTimeoutMS=2000)
    db = client['ev_database']

    # Load trạm sạc
    collection = db['ev_stations']
    stations = list(collection.find({}, {'_id': 0}))

    # Load bến phà
    ferry_collection = db['ferry_stations']
    ferries = list(ferry_collection.find({}, {'_id': 0}))

    # Load trạm dừng cao tốc
    highway_collection = db['highway_stations']
    highway_stations = list(highway_collection.find({}, {'_id': 0}))

    # Load trạm thu phí
    toll_collection = db['toll_stations']
    toll_stations = list(toll_collection.find({}, {'_id': 0}))

    # NẾU KHÔNG CÓ DỮ LIỆU → THÔNG BÁO
    if len(highway_stations) == 0:
        print(" Chưa có dữ liệu trạm cao tốc trong MongoDB")
    if len(toll_stations) == 0:
        print(" Chưa có dữ liệu trạm thu phí trong MongoDB")
        print(" Tạm thời sử dụng dữ liệu mẫu")
        toll_stations = []

    client.close()

    print(f" Đã tải {len(stations)} trạm sạc")
    print(f" Đã tải {len(ferries)} bến phà")
    print(f" Đã tải {len(highway_stations)} trạm dừng cao tốc")
    print(f" Đã tải {len(toll_stations)} trạm thu phí")

    # TẠO KD-TREE CHO TRẠM SẠC (TỐI ƯU TÌM KIẾM)
    if len(stations) > 0:
        station_coords = np.array([[s['lat'], s['lon']] for s in stations])
        station_tree = cKDTree(station_coords)
        print(" Đã khởi tạo KD-Tree cho trạm sạc")
    else:
        station_tree = None
        print(" Không có trạm sạc để tạo KD-Tree")

except Exception as e:
    print(f"❌ Lỗi kết nối MongoDB: {e}")
    stations, ferries, highway_stations, toll_stations = [], [], [], []
    station_tree = None

print("Hệ thống sẵn sàng!")
# ---------------------------------------------------------

# Geocoder
geolocator = Nominatim(user_agent="ev-app-hcm-pro-v3")

# Cấu hình EV mặc định
EV_CONFIG = {
    "battery_capacity": 75,  # kWh
    "consumption": 16.3,     # kWh/100km
    "max_range": 460,        # km
    "charge_speed_fast": 150, # kW (DC Fast)
    "charge_speed_slow": 50,  # kW (AC)
    "min_battery": 20,       # % ngưỡng an toàn
    "charge_target": 80      # % mục tiêu sạc
}

class EVState:
    """Trạng thái của xe EV trong thuật toán tìm kiếm"""
    def __init__(self, position, battery_percent, km_remaining, distance_traveled, 
                 time_spent, charging_stops, path_coords):
        self.position = position
        self.battery_percent = battery_percent
        self.km_remaining = km_remaining
        self.distance_traveled = distance_traveled
        self.time_spent = time_spent
        self.charging_stops = charging_stops
        self.path_coords = path_coords
        
    def __lt__(self, other):
        return self.time_spent < other.time_spent

# ==============================================================================
#  CÁC HÀM XỬ LÝ CHỈ DẪN (TURN-BY-TURN) - ĐÃ THÊM VÀO ĐÂY
# ==============================================================================
def get_icon_for_maneuver(m_type, modifier):
    """Trả về icon FontAwesome tương ứng hành động"""
    if m_type == "arrive": return "fa-flag-checkered"
    if m_type == "depart": return "fa-play-circle"
    if modifier and "left" in str(modifier): return "fa-arrow-left"
    if modifier and "right" in str(modifier): return "fa-arrow-right"
    if m_type == "continue" or (modifier and "straight" in modifier): return "fa-arrow-up"
    if m_type == "uturn": return "fa-undo"
    if "roundabout" in str(m_type): return "fa-sync"
    return "fa-location-arrow"

def format_turn_by_turn(steps):
    """
    Chuyển đổi các bước đi (steps) từ OSRM sang hướng dẫn tiếng Việt chi tiết.
    BỔ SUNG: Thêm thời gian di chuyển cho mỗi đoạn
    """
    instructions = []
    
    # Từ điển dịch hành động (maneuver type)
    action_map = {
        "turn": "Rẽ", "new name": "Đi tiếp vào", "depart": "Xuất phát tại", 
        "arrive": "Đến", "merge": "Nhập làn vào", "on ramp": "Vào đường dẫn", 
        "off ramp": "Ra khỏi đường dẫn", "fork": "Rẽ nhánh", "end of road": "Hết đường, rẽ", 
        "continue": "Đi thẳng", "roundabout": "Vào vòng xuyến", "rotary": "Vào vòng xoay",
        "roundabout turn": "Tại vòng xuyến", "notification": "Chú ý"
    }

    # Từ điển dịch hướng (modifier)
    modifier_map = {
        "left": "trái", "right": "phải", "sharp left": "gắt sang trái", "sharp right": "gắt sang phải",
        "slight left": "nhẹ sang trái", "slight right": "nhẹ sang phải", "straight": "thẳng", "uturn": "quay đầu"
    }

    for step in steps:
        maneuver = step.get('maneuver', {})
        m_type = maneuver.get('type')
        modifier = maneuver.get('modifier')
        name = step.get('name', '')
        distance = step.get('distance', 0)
        duration = step.get('duration', 0)  # ĐÃ THÊM: Lấy thời gian (giây)
        
        # Tạo câu lệnh cơ bản
        action_text = action_map.get(m_type, "Đi")
        direction_text = f" {modifier_map.get(modifier, '')}" if modifier else ""
        road_text = f" vào <b>{name}</b>" if name else ""
        
        # Xử lý đặc biệt cho vòng xuyến
        if "roundabout" in str(m_type) or "rotary" in str(m_type):
            exit_num = maneuver.get('exit')
            if exit_num:
                direction_text = f", ra lối thứ {exit_num}"

        # Xử lý điểm đến/đi
        if m_type == "depart":
            full_text = f"🚩 {action_text} {name}"
        elif m_type == "arrive":
            full_text = f"🏁 {action_text} {name}" if name else f"🏁 Đến điểm đích"
        else:
            full_text = f"{action_text}{direction_text}{road_text}"

        # Làm đẹp khoảng cách
        if distance < 1000:
            dist_text = f"{int(distance)}m"
        else:
            dist_text = f"{distance/1000:.1f}km"
        
        # ĐÃ THÊM: Làm đẹp thời gian
        if duration < 60:
            time_text = f"{int(duration)}s"
        else:
            time_text = f"{int(duration/60)}p"

        # Chỉ thêm vào danh sách nếu có khoảng cách hoặc là điểm đến/đi
        if distance > 0 or m_type in ['arrive', 'depart']:
            instructions.append({
                "text": full_text,
                "distance": dist_text,
                "raw_distance": distance,
                "duration": time_text,  # ĐÃ THÊM
                "raw_duration": duration,  # ĐÃ THÊM (giây)
                "icon": get_icon_for_maneuver(m_type, modifier),
                "road_name": name if name else "Đường không tên"  # ĐÃ THÊM
            })
            
    return instructions
# ==============================================================================

def create_map(center=[10.76, 106.66], zoom=12):
    """Tạo bản đồ cơ bản với tối ưu hiệu năng"""
    return folium.Map(
        location=center, 
        zoom_start=zoom,
        tiles='OpenStreetMap',
        prefer_canvas=True,  # Dùng Canvas thay vì SVG - nhanh hơn
        max_zoom=18,
        zoom_control=True,
        scrollWheelZoom=True
    )

def add_stations_to_map(map_obj, nearby_ferries=None, nearby_highway_stations=None, nearby_toll_stations=None, nearby_charging_stations=None):
    """
    TỐI ƯU HIỂN THỊ BẢN ĐỒ:
    - CHỈ HIỆN TRẠM SẠC GẦN ĐƯỜNG ĐI (không hiện tất cả)
    """
    # CHỈ HIỆN TRẠM SẠC GẦN ĐƯỜNG ĐI
    if nearby_charging_stations:
        print(f" Hiển thị {len(nearby_charging_stations)} trạm sạc gần đường đi")
        
        marker_cluster = MarkerCluster(
            name="Trạm sạc gần đường đi",
            options={
                'disableClusteringAtZoom': 10,
                'spiderfyOnMaxZoom': False,
                'maxClusterRadius': 60,
                'chunkedLoading': True
            }
        ).add_to(map_obj)

        for item in nearby_charging_stations:
            station = item['station']
            distance_to_route = item['distance_to_route']
            icon_color = "green" if station.get('type') == 'AC' else "red"
            
            popup_info = f"""
                <div style="width: 260px; font-family: Arial;">
                    <b style="color: #10b981; font-size: 13px;">⚡ TRẠM SẠC GẦN ĐƯỜNG ĐI</b><br>
                    <hr style="margin: 5px 0;">
                    <b style="color: #1e293b; font-size: 14px;">{station['name']}</b><br>
                    <div style="background: #f0fdf4; padding: 8px; margin: 8px 0; border-radius: 5px;">
                         Loại: <b>{station.get('type', 'DC')}</b><br>
                         Công suất: <b>{station.get('power', 150)} kW</b><br>
                         {station.get('address', 'N/A')}
                    </div>
                    <div style="font-size: 12px; color: #555;">
                         Cách đường đi: <b>{distance_to_route} km</b>
                    </div>
                </div>
            """
            
            folium.Marker(
                location=[station["lat"], station["lon"]],
                popup=folium.Popup(popup_info, max_width=280, lazy=True),
                icon=folium.Icon(color=icon_color, icon="bolt", prefix='fa'),
                tooltip=f" {station['name']} - {distance_to_route}km"
            ).add_to(marker_cluster)
    
    # XỬ LÝ HIỂN THỊ PHÀ
    if nearby_ferries:
        print(f" Hiển thị {len(nearby_ferries)} bến phà gần đường đi")
        
        ferry_cluster = MarkerCluster(
            name="Bến phà gần đường đi",
            options={
                'disableClusteringAtZoom': 10,
                'spiderfyOnMaxZoom': False,
                'maxClusterRadius': 60,
                'chunkedLoading': True
            }
        ).add_to(map_obj)
        
        for item in nearby_ferries:
            ferry = item['ferry']
            distance_to_route = item['distance_to_route']
            
            vehicle_allowed = ferry.get('vehicle_allowed', 'yes')
            
            if vehicle_allowed == 'yes':
                icon_color = "blue"
                status_text = "✅ Phương tiện đi qua được"
                bg_color = "#e8f5e9"
            elif vehicle_allowed == 'no':
                icon_color = "gray"
                status_text = "❌ Không cho phương tiện qua (chỉ hành khách)"
                bg_color = "#ffebee"
            else:
                icon_color = "lightblue"
                status_text = f"ℹ️ Loại: {vehicle_allowed}"
                bg_color = "#e3f2fd"
            
            popup_info = f"""
                <div style="width: 260px; font-family: Arial;">
                    <b style="color: #e74c3c; font-size: 13px;">⚠️ PHÀ GẦN ĐƯỜNG ĐI</b><br>
                    <hr style="margin: 5px 0;">
                    <b style="color: #3498db; font-size: 14px;">🚢 {ferry['name']}</b><br>
                    <div style="background: {bg_color}; padding: 8px; margin: 8px 0; border-radius: 5px;">
                        {status_text}
                    </div>
                    <div style="font-size: 12px; color: #555;">
                        📏 Cách đường đi: <b>{distance_to_route} km</b>
                    </div>
                </div>
            """
            
            folium.Marker(
                location=[ferry["lat"], ferry["lon"]],
                popup=popup_info,
                icon=folium.Icon(color=icon_color, icon="ship", prefix='fa'),
                tooltip=f"Phà: {ferry['name']} ({distance_to_route}km)"
            ).add_to(ferry_cluster)
    
    # XỬ LÝ HIỂN THỊ TRẠM DỪNG CAO TỐC
    if nearby_highway_stations:
        print(f"🛣️ Hiển thị {len(nearby_highway_stations)} trạm dừng cao tốc gần đường đi")
        
        highway_cluster = MarkerCluster(
            name="Trạm dừng cao tốc",
            options={
                'disableClusteringAtZoom': 9,
                'spiderfyOnMaxZoom': False,
                'maxClusterRadius': 50,
                'chunkedLoading': True
            }
        ).add_to(map_obj)
        
        for item in nearby_highway_stations:
            station = item['station']
            distance_to_route = item['distance_to_route']
            
            amenities = station.get('amenities', [])
            amenities_text = ', '.join(amenities) if amenities else 'Không có thông tin'
            
            popup_info = f"""
                <div style="width: 260px; font-family: Arial;">
                    <b style="color: #f97316; font-size: 13px;">🛣️ TRẠM DỪNG CAO TỐC</b><br>
                    <hr style="margin: 5px 0;">
                    <b style="color: #1e293b; font-size: 14px;">{station['name']}</b><br>
                    <div style="background: #fff7ed; padding: 8px; margin: 8px 0; border-radius: 5px;">
                        🛣️ Cao tốc: <b>{station.get('highway', 'N/A')}</b><br>
                        🏪 Tiện ích: {amenities_text}
                    </div>
                    <div style="font-size: 12px; color: #555;">
                        📏 Cách đường đi: <b>{distance_to_route} km</b>
                    </div>
                </div>
            """
            
            folium.Marker(
                location=[station["lat"], station["lon"]],
                popup=popup_info,
                icon=folium.Icon(color="orange", icon="gas-pump", prefix='fa', icon_size=(12, 12)),
                tooltip=f"⛽ Trạm: {station['name']} - Cách {distance_to_route}km"
            ).add_to(highway_cluster)
    
    # XỬ LÝ HIỂN THỊ TRẠM THU PHÍ
    if nearby_toll_stations:
        print(f"💰 Hiển thị {len(nearby_toll_stations)} trạm thu phí gần đường đi")
        
        toll_cluster = MarkerCluster(
            name="Trạm thu phí",
            options={
                'disableClusteringAtZoom': 9,
                'spiderfyOnMaxZoom': False,
                'maxClusterRadius': 50,
                'chunkedLoading': True
            }
        ).add_to(map_obj)
        
        for item in nearby_toll_stations:
            station = item['station']
            distance_to_route = item['distance_to_route']
            
            toll_fee = station.get('toll_fee', 'Chưa rõ')
            highway = station.get('highway', 'N/A')
            
            popup_info = f"""
                <div style="width: 260px; font-family: Arial;">
                    <b style="color: #dc2626; font-size: 13px;">💰 TRẠM THU PHÍ</b><br>
                    <hr style="margin: 5px 0;">
                    <b style="color: #1e293b; font-size: 14px;">{station['name']}</b><br>
                    <div style="background: #fef2f2; padding: 8px; margin: 8px 0; border-radius: 5px;">
                        🛣️ Tuyến: <b>{highway}</b><br>
                        💵 Phí: <b>{toll_fee}</b>
                    </div>
                    <div style="font-size: 12px; color: #555;">
                        📏 Cách đường đi: <b>{distance_to_route} km</b>
                    </div>
                </div>
            """
            
            folium.Marker(
                location=[station["lat"], station["lon"]],
                popup=popup_info,
                icon=folium.Icon(color="red", icon="money-bill", prefix='fa', icon_size=(12, 12)),
                tooltip=f"💰 {station['name']} - {distance_to_route}km"
            ).add_to(toll_cluster)
            
def get_route(start_coords, end_coords, options=None):
    """Lấy route từ OSRM API với fallback - ĐÃ CẢI THIỆN"""
    
    # THÊM NHIỀU SERVER HƠN
    osrm_servers = [
        "http://router.project-osrm.org",
        "https://routing.openstreetmap.de/routed-car",
        "http://router.project-osrm.org",  # Thử lại server 1
        "https://router.openstreetmap.de/routed-foot"  # Backup server
    ]
    
    params = {
        "overview": "full",
        "geometries": "polyline",
        "steps": "true",
        "alternatives": "1"
    }
    
    # Xử lý bộ lọc tránh phà
    excludes = []
    if options:
        if options.get('avoid_highways'): excludes.append('motorway')
        if options.get('avoid_ferries'): excludes.append('ferry')
    
    # Tính khoảng cách để điều chỉnh timeout
    distance_km = geodesic(start_coords, end_coords).km
    timeout_seconds = 30 if distance_km < 500 else 45  # Tăng timeout cho route dài
    
    print(f"📏 Khoảng cách ước tính: {distance_km:.0f}km - Timeout: {timeout_seconds}s")
    
    for server_idx, base_url in enumerate(osrm_servers):
        try:
            url = f"{base_url}/route/v1/driving/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"
            
            if excludes:
                params['exclude'] = ','.join(excludes)
            
            print(f"🔄 Thử server {server_idx + 1}/{len(osrm_servers)}: {base_url}")
            
            # THÊM DELAY GIỮA CÁC REQUEST để tránh rate limit
            if server_idx > 0:
                time.sleep(2)  # Đợi 2s trước khi thử server tiếp theo
            
            # Thêm headers để server không chặn
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json'
            }
            
            response = requests.get(url, params=params, timeout=timeout_seconds, headers=headers)
            
            # LOG CHI TIẾT HƠN
            print(f"   📡 Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"   📦 Response code: {data.get('code', 'N/A')}")
                
                if data.get("code") == "Ok" and data.get("routes"):
                    routes = []
                    for route in data["routes"][:2]:
                        routes.append({
                            "geometry": route["geometry"],
                            "distance": route["distance"],
                            "duration": route["duration"],
                            "steps": route.get("legs", [{}])[0].get("steps", [])
                        })
                    print(f"✅ Nhận được {len(routes)} route(s) từ {base_url}")
                    return routes
                else:
                    # Log lỗi từ OSRM
                    error_msg = data.get("message", "Unknown error")
                    print(f"   ⚠️ OSRM error: {error_msg}")
            elif response.status_code == 400:
                # Bad request - có thể do tham số sai
                print(f"   ❌ Bad request (400) - Thử bỏ exclude filter...")
            elif response.status_code == 503:
                print(f"   ⏳ Service unavailable (503) - Server quá tải")
            else:
                print(f"   ⚠️ Unexpected status: {response.status_code}")
                print(f"   📄 Response: {response.text[:200]}")
            
            # Nếu thất bại với exclude, thử lại không có exclude
            if 'exclude' in params and response.status_code != 200:
                print(f"   🔄 Thử lại không có bộ lọc exclude...")
                params_no_exclude = {k: v for k, v in params.items() if k != 'exclude'}
                
                response = requests.get(url, params=params_no_exclude, timeout=timeout_seconds)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        routes = []
                        for route in data["routes"][:2]:
                            routes.append({
                                "geometry": route["geometry"],
                                "distance": route["distance"],
                                "duration": route["duration"],
                                "steps": route.get("legs", [{}])[0].get("steps", [])
                            })
                        print(f"✅ Nhận được {len(routes)} route(s) (không có bộ lọc)")
                        return routes
                        
        except requests.exceptions.Timeout:
            print(f"⏱️ Server {base_url} timeout sau {timeout_seconds}s")
            continue
        except requests.exceptions.ConnectionError as e:
            print(f"🔌 Connection error: {str(e)[:100]}")
            continue
        except requests.exceptions.RequestException as e:
            print(f"❌ Request error: {str(e)[:100]}")
            continue
        except Exception as e:
            print(f"❌ Server {base_url} lỗi: {str(e)[:100]}")
            traceback.print_exc()
            continue
    
    # NẾU TẤT CẢ SERVER ĐỀU THẤT BẠI
    print("❌ Tất cả OSRM servers đều thất bại")
    print("💡 Gợi ý:")
    if distance_km > 500:
        print(f"   • Route rất dài ({distance_km:.0f}km) - OSRM public servers có thể từ chối")
        print("   • Thử chia nhỏ chuyến đi thành nhiều đoạn ngắn hơn (< 500km)")
    else:
        print("   • Kiểm tra kết nối Internet")
        print("   • OSRM servers có thể đang bảo trì hoặc quá tải")
        print("   • Thử lại sau vài phút")
    
    return []

def calculate_battery_consumption(distance_km, config):
    kwh_used = (distance_km / 100) * config['consumption']
    return (kwh_used / config['battery_capacity']) * 100

def calculate_km_from_battery(battery_percent, config):
    kwh_available = (battery_percent / 100) * config['battery_capacity']
    return (kwh_available / config['consumption']) * 100

def find_nearest_reachable_station(current_pos, battery_percent, config):
    km_can_travel = calculate_km_from_battery(battery_percent, config)
    
    # Kiểm tra nếu không có station_tree
    if station_tree is None or len(stations) == 0:
        print("⚠️ Không có trạm sạc trong hệ thống!")
        return []
    
    # Tìm 10 trạm gần nhất bằng KD-Tree
    try:
        distances, indices = station_tree.query(current_pos, k=min(10, len(stations)))
    except Exception as e:
        print(f"⚠️ Lỗi KD-Tree query: {e}")
        return []
    
    for dist_deg, idx in zip(distances, indices):
        station = stations[idx]
        distance = geodesic(current_pos, (station['lat'], station['lon'])).km
        
        if distance < km_can_travel:
            battery_needed = calculate_battery_consumption(distance, config)
            if battery_percent - battery_needed >= 5:
                return [{
                    'station': station,
                    'distance': distance,
                    'battery_on_arrival': battery_percent - battery_needed
                }]
    
    return []

def calculate_charge_time(current_battery, target_battery, power_kw, config):
    charge_amount = target_battery - current_battery
    energy_kwh = (charge_amount / 100) * config['battery_capacity']
    
    if target_battery <= 80:
        hours = energy_kwh / power_kw
    else:
        hours = (energy_kwh / power_kw) * 1.5 # Sạc chậm khi > 80%
    
    return hours * 60

def heuristic(current_pos, goal_pos):
    return geodesic(current_pos, goal_pos).km

def a_star_ev_routing(start_coords, end_coords, route_coords, start_battery, config, priority='time'):
    route_distance = geodesic(start_coords, end_coords).km * 1.3
    battery_percent = start_battery
    charging_stops = []
    total_distance = 0
    
    # TĂNG SỐ KM MỖI CHECKPOINT ĐỂ GIẢM SỐ LƯỢNG TÍNH TOÁN
    num_checkpoints = max(2, int(route_distance / 150))  # Mỗi 150km 1 checkpoint (thay vì 100)
    checkpoint_step = max(1, len(route_coords) // num_checkpoints)
    checkpoints = [start_coords]
    for i in range(checkpoint_step, len(route_coords), checkpoint_step):
        checkpoints.append(route_coords[i])
    checkpoints.append(end_coords)
    
    print(f"⚡ Tối ưu: {len(checkpoints)} checkpoints cho {route_distance:.0f}km")
    
    for i in range(len(checkpoints) - 1):
        segment_dist = geodesic(checkpoints[i], checkpoints[i+1]).km
        battery_needed = calculate_battery_consumption(segment_dist, config)
        
        # Kiểm tra nếu pin không đủ để đi đến checkpoint tiếp theo
        if battery_percent - battery_needed < config['min_battery']:
            reachable = find_nearest_reachable_station(checkpoints[i], battery_percent, config)
            if not reachable:
                return None, f"Không đủ pin! Pin hiện tại: {battery_percent:.1f}%, cần {battery_needed:.1f}% để đi {segment_dist:.1f}km tiếp theo"
            
            station = reachable[0]['station']
            battery_on_arrival = reachable[0]['battery_on_arrival']
            
            # ========== TỐI ƯU THÔNG MINH: CHỈ SẠC VỪA ĐỦ =========
            target_battery = calculate_optimal_charge_target(
                checkpoints[i:],  # Các điểm còn lại
                station,
                battery_on_arrival,
                config
            )
            
            charge_time = calculate_charge_time(battery_on_arrival, target_battery, station.get('power', 150), config)
            
            charging_stops.append({
                'station': station,
                'arrival_battery': round(battery_on_arrival, 1),
                'start_charge': round(battery_on_arrival, 1),
                'end_charge': target_battery,
                'charge_time': round(charge_time, 1),
                'distance_from_start': round(total_distance + reachable[0]['distance'], 1)
            })
            
            battery_percent = target_battery
            total_distance += reachable[0]['distance']
            
            segment_dist = geodesic((station['lat'], station['lon']), checkpoints[i+1]).km
            battery_needed = calculate_battery_consumption(segment_dist, config)
        
        battery_percent -= battery_needed
        total_distance += segment_dist
        
        if battery_percent < 0:
            return None, f"Pin bị âm tại km {total_distance:.1f}. Cần thêm trạm sạc!"
    
    driving_time = (route_distance / 60) * 60
    charging_time = sum(s['charge_time'] for s in charging_stops)
    
    return {
        'total_distance': round(route_distance, 2),
        'driving_time': round(driving_time, 1),
        'charging_time': round(charging_time, 1),
        'total_time': round(driving_time + charging_time, 1),
        'num_charges': len(charging_stops),
        'charging_stops': charging_stops,
        'final_battery': round(max(0, battery_percent), 1),
        'feasible': True
    }, None

def calculate_optimal_charge_target(remaining_checkpoints, current_station, current_battery, config):
    """
    TỐI ƯU THÔNG MINH: TÍNH LƯỢNG SẠC TỐI ƯU
    
    Logic:
    1. Tìm trạm tiếp theo trong vòng 200km
    2. Nếu trạm đó tốt hơn (công suất cao) → Chỉ sạc vừa đủ đến đó
    3. Nếu không có trạm tốt hơn → Sạc đầy để tối đa hóa tầm xa
    
    Args:
        remaining_checkpoints: Các điểm còn lại trên route
        current_station: Trạm hiện tại
        current_battery: Pin hiện tại (%)
        config: Cấu hình xe
    
    Returns:
        Mức pin mục tiêu (%)
    """
    if len(remaining_checkpoints) < 2:
        # Gần đích → Sạc vừa đủ để đến đích
        return min(100, current_battery + 30)
    
    # Tính khoảng cách đến các checkpoints tiếp theo
    current_pos = remaining_checkpoints[0]
    max_search_distance = 200  # km
    
    # Tìm trạm tiếp theo tốt hơn
    next_better_station = find_next_better_station(
        current_pos, 
        max_search_distance, 
        current_station.get('power', 150),
        config
    )
    
    if next_better_station:
        station_distance = next_better_station['distance']
        station_power = next_better_station['station'].get('power', 150)
        current_power = current_station.get('power', 150)
        
        # Tính pin cần để đến trạm đó (+ buffer 10%)
        battery_to_next = calculate_battery_consumption(station_distance, config)
        target = current_battery + battery_to_next + 10
        
        # CHỈ SẠC VỪA ĐỦ NẾU:
        # 1. Trạm sau công suất cao hơn ít nhất 30kW
        # 2. Cần sạc < 80% (tránh sạc chậm)
        if station_power >= current_power + 30 and target <= 80:
            print(f"   💡 Tối ưu: Chỉ sạc {target:.0f}% để đến trạm {next_better_station['station']['name']} ({station_power}kW)")
            return min(85, max(target, current_battery + 20))
    
    # Không có trạm tốt hơn → Sạc đầy
    return 100

def find_next_better_station(current_pos, max_distance_km, current_power, config):
    """
    Tìm trạm tiếp theo tốt hơn trong vòng max_distance_km
    
    Args:
        current_pos: Vị trí hiện tại
        max_distance_km: Khoảng cách tìm kiếm tối đa
        current_power: Công suất trạm hiện tại
        config: Cấu hình xe
    
    Returns:
        {station, distance} hoặc None
    """
    if station_tree is None or len(stations) == 0:
        return None
    
    # Tìm 20 trạm gần nhất
    try:
        distances, indices = station_tree.query(current_pos, k=min(20, len(stations)))
    except Exception as e:
        return None
    
    better_stations = []
    
    for dist_deg, idx in zip(distances, indices):
        station = stations[idx]
        distance = geodesic(current_pos, (station['lat'], station['lon'])).km
        
        # CHỈ XÉT TRẠM:
        # 1. Trong vòng max_distance_km
        # 2. Công suất cao hơn ít nhất 30kW
        if distance <= max_distance_km and station.get('power', 150) >= current_power + 30:
            better_stations.append({
                'station': station,
                'distance': distance,
                'power': station.get('power', 150)
            })
    
    if not better_stations:
        return None
    
    # Ưu tiên trạm gần + công suất cao
    # Score = power * 1.5 - distance * 2
    better_stations.sort(key=lambda x: x['power'] * 1.5 - x['distance'] * 2, reverse=True)
    return better_stations[0]

def add_route_to_map(map_obj, route_coords, charging_stops, color="blue", label=""):
    """Thêm route - TỐI ƯU CỰC MẠNH"""
    # GIẢM SỐ ĐIỂM VẼ ĐƯỜNG - CHỈ 100 ĐIỂM THAY VÌ 300
    step = max(1, len(route_coords) // 100)
    sampled_coords = route_coords[::step]
    
    folium.PolyLine(
        sampled_coords, 
        color=color, 
        weight=5,  # Giảm weight
        opacity=0.7,  # Giảm opacity
        popup=label, 
        smooth_factor=3.0  # Tăng smooth_factor
    ).add_to(map_obj)
    
    # Điểm xuất phát
    folium.Marker(
        route_coords[0], 
        icon=folium.Icon(color="green", icon="play", prefix='fa'),
        popup=folium.Popup("<b>Xuất phát</b>", max_width=150)
    ).add_to(map_obj)
    
    # Điểm đến
    folium.Marker(
        route_coords[-1], 
        icon=folium.Icon(color="red", icon="flag-checkered", prefix='fa'),
        popup=folium.Popup("<b>Đích</b>", max_width=150)
    ).add_to(map_obj)
    
    # TRẠM SẠC - BỎ CIRCLE ĐỂ NHANH HƠN
    for i, stop in enumerate(charging_stops, 1):
        s = stop['station']
        
        popup_content = f"""
            <div style='min-width: 200px; font-family: Arial;'>
                <b style='color: #f97316;'>TRẠM #{i}</b><br>
                <b>{s['name']}</b><br>
                <small>{s.get('power', 150)}kW | {stop['charge_time']}p</small>
            </div>
        """
        
        folium.Marker(
            [s['lat'], s['lon']],
            icon=folium.Icon(color="orange", icon="bolt", prefix='fa'),
            popup=folium.Popup(popup_content, max_width=220),
            tooltip=f"Trạm #{i}"
        ).add_to(map_obj)
        
def create_comparison_chart(routes_data):
    if not routes_data: return None
    plt.rcParams['font.family'] = 'DejaVu Sans'
    plt.rcParams['font.size'] = 10
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), dpi=80)
    fig.patch.set_facecolor('white')
    fig.patch.set_alpha(0.95)
    
    names = [r['name'] for r in routes_data]
    colors = ['#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b']  # Màu cho từng route
    x = np.arange(len(names))
    
    # 1. Quãng đường - Line chart
    dists = [r['metrics']['total_distance'] for r in routes_data]
    axes[0,0].plot(x, dists, marker='o', linewidth=3, markersize=10, color='#3b82f6', 
                   markerfacecolor='white', markeredgewidth=2, markeredgecolor='#3b82f6')
    axes[0,0].fill_between(x, dists, alpha=0.2, color='#3b82f6')
    axes[0,0].set_title('Quãng đường (km)', fontweight='bold', pad=15, fontsize=12)
    axes[0,0].set_ylabel('km', fontweight='bold')
    axes[0,0].set_xticks(x)
    axes[0,0].set_xticklabels(names, rotation=0)
    axes[0,0].grid(axis='y', alpha=0.3, linestyle='--')
    # Thêm giá trị trên điểm
    for i, v in enumerate(dists):
        axes[0,0].text(i, v + max(dists)*0.02, f'{v:.1f}', ha='center', fontweight='bold', fontsize=9)
    
    # 2. Thời gian - Stacked area chart
    d_time = [r['metrics']['driving_time'] for r in routes_data]
    c_time = [r['metrics']['charging_time'] for r in routes_data]
    axes[0,1].plot(x, d_time, marker='o', linewidth=3, markersize=8, color='#10b981', 
                   label='Lái xe', markerfacecolor='white', markeredgewidth=2, markeredgecolor='#10b981')
    axes[0,1].plot(x, c_time, marker='s', linewidth=3, markersize=8, color='#f59e0b', 
                   label='Sạc pin', markerfacecolor='white', markeredgewidth=2, markeredgecolor='#f59e0b')
    axes[0,1].fill_between(x, d_time, alpha=0.2, color='#10b981')
    axes[0,1].fill_between(x, c_time, alpha=0.2, color='#f59e0b')
    axes[0,1].set_xticks(x)
    axes[0,1].set_xticklabels(names, rotation=0)
    axes[0,1].legend(loc='upper right', framealpha=0.9)
    axes[0,1].set_title('Thời gian (phút)', fontweight='bold', pad=15, fontsize=12)
    axes[0,1].set_ylabel('phút', fontweight='bold')
    axes[0,1].grid(axis='y', alpha=0.3, linestyle='--')

    # 3. Số lần sạc - Line chart với Y axis số nguyên
    charges = [r['metrics']['num_charges'] for r in routes_data]
    axes[1,0].plot(x, charges, marker='D', linewidth=3, markersize=10, color='#8b5cf6',
                   markerfacecolor='white', markeredgewidth=2, markeredgecolor='#8b5cf6')
    axes[1,0].fill_between(x, charges, alpha=0.2, color='#8b5cf6')
    axes[1,0].set_title('Số lần sạc', fontweight='bold', pad=15, fontsize=12)
    axes[1,0].set_ylabel('lần', fontweight='bold')
    axes[1,0].set_xticks(x)
    axes[1,0].set_xticklabels(names, rotation=0)
    axes[1,0].grid(axis='y', alpha=0.3, linestyle='--')
    
    # QUAN TRỌNG: Set Y axis chỉ hiển thị số nguyên
    max_charges = max(charges) if charges else 1
    axes[1,0].set_ylim(bottom=0, top=max_charges + 1)
    axes[1,0].yaxis.set_major_locator(plt.MaxNLocator(integer=True))  # Chỉ số nguyên
    
    # Thêm giá trị
    for i, v in enumerate(charges):
        axes[1,0].text(i, v + 0.15, f'{v}', ha='center', fontweight='bold', fontsize=10)

    # 4. Tổng thời gian - Line chart
    total = [r['metrics']['total_time'] for r in routes_data]
    axes[1,1].plot(x, total, marker='*', linewidth=3, markersize=15, color='#ef4444',
                   markerfacecolor='white', markeredgewidth=2, markeredgecolor='#ef4444')
    axes[1,1].fill_between(x, total, alpha=0.2, color='#ef4444')
    axes[1,1].set_title('Tổng thời gian (phút)', fontweight='bold', pad=15, fontsize=12)
    axes[1,1].set_ylabel('phút', fontweight='bold')
    axes[1,1].set_xticks(x)
    axes[1,1].set_xticklabels(names, rotation=0)
    axes[1,1].grid(axis='y', alpha=0.3, linestyle='--')
    # Thêm giá trị
    for i, v in enumerate(total):
        axes[1,1].text(i, v + max(total)*0.02, f'{v:.0f}', ha='center', fontweight='bold', fontsize=9)
    
    plt.tight_layout(pad=2.5)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=80, facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"

def summarize_route_segments(instructions):
    """
    Tổng hợp thông tin các đoạn đường chính
    Trả về danh sách các đường quan trọng với tổng khoảng cách và thời gian
    """
    if not instructions:
        return []
    
    segments = []
    current_road = None
    current_distance = 0
    current_duration = 0
    
    for inst in instructions:
        road_name = inst.get('road_name', 'Đường không tên')
        
        # Bỏ qua điểm xuất phát và đích
        if inst['icon'] in ['fa-play-circle', 'fa-flag-checkered']:
            if current_road and current_distance > 100:  # Chỉ lưu đoạn > 100m
                segments.append({
                    'road': current_road,
                    'distance': current_distance,
                    'duration': current_duration,
                    'distance_text': f"{current_distance/1000:.1f}km" if current_distance >= 1000 else f"{int(current_distance)}m",
                    'duration_text': f"{int(current_duration/60)}p" if current_duration >= 60 else f"{int(current_duration)}s"
                })
            continue
        
        # Nếu đổi đường hoặc đường mới
        if road_name != current_road:
            # Lưu đường cũ nếu có và đủ dài
            if current_road and current_distance > 100:
                segments.append({
                    'road': current_road,
                    'distance': current_distance,
                    'duration': current_duration,
                    'distance_text': f"{current_distance/1000:.1f}km" if current_distance >= 1000 else f"{int(current_distance)}m",
                    'duration_text': f"{int(current_duration/60)}p" if current_duration >= 60 else f"{int(current_duration)}s"
                })
            
            # Bắt đầu đường mới
            current_road = road_name
            current_distance = inst.get('raw_distance', 0)
            current_duration = inst.get('raw_duration', 0)
        else:
            # Cộng dồn cùng đường
            current_distance += inst.get('raw_distance', 0)
            current_duration += inst.get('raw_duration', 0)
    
    # Lưu đoạn cuối
    if current_road and current_distance > 100:
        segments.append({
            'road': current_road,
            'distance': current_distance,
            'duration': current_duration,
            'distance_text': f"{current_distance/1000:.1f}km" if current_distance >= 1000 else f"{int(current_distance)}m",
            'duration_text': f"{int(current_duration/60)}p" if current_duration >= 60 else f"{int(current_duration)}s"
        })
    
    return segments

def create_route_detail_chart(route_data):
    if not route_data['stops']: return None
    plt.rcParams['font.family'] = 'DejaVu Sans'
    plt.rcParams['font.size'] = 11
    fig, ax = plt.subplots(figsize=(14, 7), dpi=90)
    fig.patch.set_facecolor('white')
    fig.patch.set_alpha(0.95)
    
    stops = route_data['stops']
    names = [f"Tram {i+1}\n{s['station']['name'][:15]}..." if len(s['station']['name']) > 15 
             else f"Tram {i+1}\n{s['station']['name']}" for i, s in enumerate(stops)]
    arrival = [s['arrival_battery'] for s in stops]
    departure = [s['end_charge'] for s in stops]
    
    x = np.arange(len(names))
    width = 0.35
    
    # Vẽ 2 cột: Pin khi đến, Pin sau sạc
    bars1 = ax.bar(x - width/2, arrival, width, label='Pin khi đến', 
                   color='#ef4444', alpha=0.85, edgecolor='white', linewidth=2.5)
    bars2 = ax.bar(x + width/2, departure, width, label='Pin sau sạc', 
                   color='#10b981', alpha=0.85, edgecolor='white', linewidth=2.5)
    
    ax.set_xlabel('Trạm sạc', fontweight='bold', fontsize=14, labelpad=15)
    ax.set_ylabel('Mức pin (%)', fontweight='bold', fontsize=14)
    ax.set_title('Biểu đồ chi tiết pin tại các trạm sạc', fontweight='bold', pad=25, fontsize=17)
    ax.set_xticks(x)
    ax.set_xticklabels(names, fontsize=11, ha='center')
    ax.legend(loc='upper right', framealpha=0.95, fontsize=13, ncol=2)
    ax.grid(axis='y', alpha=0.3, linestyle='--', linewidth=0.8)
    ax.set_ylim([0, 110])
    
    # Thêm đường kẻ ngang tại 100%
    ax.axhline(y=100, color='#64748b', linestyle='--', linewidth=2, alpha=0.4)
    if len(names) > 0:
        ax.text(len(names)-0.5, 103, '100%', fontsize=11, color='#64748b', fontweight='bold')
    
    # Thêm giá trị trên các cột
    for bar in bars1:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height + 2.5,
               f'{height:.0f}%', ha='center', va='bottom', 
               fontsize=12, fontweight='bold', color='#ef4444')
    
    for bar in bars2:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height + 2.5,
               f'{height:.0f}%', ha='center', va='bottom', 
               fontsize=12, fontweight='bold', color='#10b981')
    
    plt.tight_layout(pad=2.5)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=90, facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'avt.ico', mimetype='image/x-icon')

@app.route('/')
def index():
    return render_template('index.html', ev_config=EV_CONFIG)

@app.route('/route', methods=['POST'])
def find_route():
    data = request.json
    start_addr, end_addr = data.get('start'), data.get('end')
    
    try:
        print(f"\n{'='*60}")
        print(f"🔍 TÌM KIẾM LỘ TRÌNH")
        print(f"{'='*60}")
        print(f"📍 Từ: {start_addr}")
        print(f"📍 Đến: {end_addr}")
        
        total_start = time.time()
        
        # GEOCODING
        geocode_start = time.time()
        print(f"🔄 Đang geocode '{start_addr}'...")
        start_loc = geolocator.geocode(start_addr + ", Việt Nam", timeout=15)
        
        if not start_loc:
            print(f"❌ Không tìm thấy địa chỉ xuất phát: {start_addr}")
            return jsonify({"error": f"Không tìm thấy địa chỉ xuất phát: {start_addr}"}), 400
        
        print(f"🔄 Đang geocode '{end_addr}'...")
        end_loc = geolocator.geocode(end_addr + ", Việt Nam", timeout=15)
        
        if not end_loc:
            print(f"❌ Không tìm thấy địa chỉ đến: {end_addr}")
            return jsonify({"error": f"Không tìm thấy địa chỉ đến: {end_addr}"}), 400
        
        print(f"⏱️ Geocoding: {time.time() - geocode_start:.1f}s")
        
        start_coords = (start_loc.latitude, start_loc.longitude)
        end_coords = (end_loc.latitude, end_loc.longitude)
        
        print(f"✅ Start: {start_coords[0]:.4f}, {start_coords[1]:.4f}")
        print(f"✅ End: {end_coords[0]:.4f}, {end_coords[1]:.4f}")
        
        direct_distance = geodesic(start_coords, end_coords).km
        print(f"📏 Khoảng cách đường chim bay: {direct_distance:.0f}km")
        
        if direct_distance < 1:
            return jsonify({"error": "Hai điểm quá gần (< 1km)"}), 400
        
        if direct_distance > 1500:
            print(f"⚠️ CẢNH BÁO: Quãng đường rất xa ({direct_distance:.0f}km)")
        
        # GET ROUTE
        route_start = time.time()
        print(f"🔄 Đang lấy route từ OSRM...")
        base_routes = get_route(start_coords, end_coords, data)
        print(f"⏱️ OSRM API: {time.time() - route_start:.1f}s")
        
        if not base_routes:
            print(f"❌ OSRM không trả về route nào")
            return jsonify({
                "error": f"Không thể tìm đường đi.\n\n💡 Nguyên nhân:\n• Quãng đường quá xa ({direct_distance:.0f}km)\n• OSRM timeout\n• Không có đường kết nối"
            }), 400
        
        print(f"✅ Nhận được {len(base_routes)} route từ OSRM")
        
        # TÍNH TOÁN TRẠM SẠC
        calc_start = time.time()
        routes_data = []
        route_names = ['Lộ trình tối ưu', 'Lộ trình thay thế']
        route_colors = ['#3b82f6', '#94a3b8']
        
        for idx, base_route in enumerate(base_routes[:2]):
            try:
                user_config = EV_CONFIG.copy()
                
                if data.get('battery_capacity'):
                    user_config['battery_capacity'] = float(data['battery_capacity'])
                if data.get('consumption'):
                    user_config['consumption'] = float(data['consumption'])
                if data.get('max_range'):
                    user_config['max_range'] = float(data['max_range'])
                if data.get('charge_speed_fast'):
                    user_config['charge_speed_fast'] = float(data['charge_speed_fast'])
                if data.get('min_battery'):
                    user_config['min_battery'] = float(data['min_battery'])
                
                route_coords = polyline.decode(base_route['geometry'])
                route_distance_km = base_route['distance'] / 1000
                
                print(f"\n📍 Đang xử lý route {idx+1}:")
                print(f"   - Quãng đường: {route_distance_km:.1f}km")
                print(f"   - Số điểm: {len(route_coords)}")
                
                result, err = a_star_ev_routing(
                    start_coords, 
                    end_coords, 
                    route_coords, 
                    float(data.get('start_battery', 100)), 
                    user_config, 
                    priority='time'
                )
                
                if result:
                    print(f"   ✅ Khả thi: {result['num_charges']} lần sạc, {result['total_time']:.0f}p")
                    
                    raw_steps = base_route.get('steps', [])
                    turn_by_turn = format_turn_by_turn(raw_steps)
                    road_segments = summarize_route_segments(turn_by_turn)
                    
                    routes_data.append({
                        'name': route_names[idx],
                        'route': base_route, 
                        'coords': route_coords,
                        'stops': result['charging_stops'], 
                        'metrics': result, 
                        'feasible': True,
                        'color': route_colors[idx],
                        'is_best': False,
                        'instructions': turn_by_turn,
                        'segments': road_segments
                    })
                else:
                    print(f"   ❌ Không khả thi: {err}")
            except Exception as e:
                print(f"   ❌ Lỗi xử lý route {idx}: {e}")
                traceback.print_exc()
                continue
        
        print(f"⏱️ Tính trạm sạc: {time.time() - calc_start:.1f}s")
        
        if not routes_data:
            print(f"❌ KHÔNG CÓ LỘ TRÌNH KHẢ THI")
            current_battery = float(data.get('start_battery', 100))
            km_can_travel = calculate_km_from_battery(current_battery, EV_CONFIG)
            
            return jsonify({
                "error": f"Không có lộ trình khả thi.\n\n📊 Thông tin:\n• Quãng đường: {direct_distance:.0f}km\n• Pin: {current_battery}%\n• Đi được: {km_can_travel:.0f}km\n• Thiếu: {direct_distance - km_can_travel:.0f}km"
            }), 400
        
        routes_data.sort(key=lambda x: x['metrics']['total_time'])
        routes_data[0]['is_best'] = True
        
        print(f"\n✅ Có {len(routes_data)} lộ trình khả thi")
        
        # TÌM TRẠM GẦN ĐƯỜNG ĐI
        nearby_start = time.time()
        best_route_coords = routes_data[0]['coords']
        
        nearby_charging_stations = find_charging_stations_near_route(best_route_coords, max_distance_km=5)
        
        avoid_ferries = data.get('avoid_ferries', False)
        avoid_highways = data.get('avoid_highways', False)
        avoid_tolls = data.get('avoid_tolls', False)
        
        nearby_ferries = find_ferries_near_route(best_route_coords, max_distance_km=5) if avoid_ferries else []
        nearby_highway_stations = find_highway_stations_near_route(best_route_coords, max_distance_km=3) if not avoid_highways else []
        nearby_toll_stations = find_toll_stations_near_route(best_route_coords, max_distance_km=2) if avoid_tolls else []
        
        print(f"⏱️ Tìm trạm gần đường: {time.time() - nearby_start:.1f}s")
        
        # TẠO BẢN ĐỒ
        map_start = time.time()
        center_lat = (start_coords[0] + end_coords[0]) / 2
        center_lon = (start_coords[1] + end_coords[1]) / 2
        m = create_map(center=[center_lat, center_lon], zoom=8)
        
        add_stations_to_map(
            m,
            nearby_charging_stations=nearby_charging_stations,
            nearby_ferries=nearby_ferries if avoid_ferries else None,
            nearby_highway_stations=nearby_highway_stations if not avoid_highways else None,
            nearby_toll_stations=nearby_toll_stations if avoid_tolls else None
        )
        
        for r in reversed(routes_data):
            add_route_to_map(m, r['coords'], r['stops'], r['color'], r['name'])
        
        # LEGEND TO HƠN VÀ RÕ RÀNG HƠN
        legend_html = '''
        <div style="position: fixed; bottom: 20px; right: 20px; 
                    background: white; padding: 18px 22px; border-radius: 12px;
                    box-shadow: 0 6px 20px rgba(0,0,0,0.35); z-index: 9999;
                    font-size: 13px; width: 340px; border: 3px solid #2e7d32;
                    font-family: 'Inter', Arial, sans-serif;">
            <b style="font-size: 16px; color: #1b5e20; display: block; margin-bottom: 10px; 
                      text-align: center; border-bottom: 3px solid #2e7d32; padding-bottom: 8px;">
                🗺️ Chú thích bản đồ
            </b>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; line-height: 2;">
                <div style="font-size: 12px;">
                    <i class="fa fa-play" style="color: green; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">Xuất phát</span>
                </div>
                <div style="font-size: 12px;">
                    <i class="fa fa-flag-checkered" style="color: red; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">Đích</span>
                </div>
                
                <div style="font-size: 12px;">
                    <div style="height: 3px; width: 30px; background: #3b82f6; display: inline-block; vertical-align: middle;"></div> 
                    <span style="font-weight: 600;">Tối ưu</span>
                </div>
                <div style="font-size: 12px;">
                    <div style="height: 3px; width: 30px; background: #94a3b8; display: inline-block; vertical-align: middle;"></div> 
                    <span style="font-weight: 600;">Thay thế</span>
                </div>
                
                <div style="font-size: 12px;">
                    <i class="fa fa-bolt" style="color: orange; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">Đã chọn sạc</span>
                </div>
                <div style="font-size: 12px;">
                    <i class="fa fa-bolt" style="color: green; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">AC</span>
                </div>
                
                <div style="font-size: 12px;">
                    <i class="fa fa-bolt" style="color: red; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">DC</span>
                </div>
                <div style="font-size: 12px;">
                    <i class="fa fa-ship" style="color: blue; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">Phà</span>
                </div>
                
                <div style="font-size: 12px;">
                    <i class="fa fa-gas-pump" style="color: orange; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">Cao tốc</span>
                </div>
                <div style="font-size: 12px;">
                    <i class="fa fa-money-bill" style="color: red; width: 18px; font-size: 12px;"></i> 
                    <span style="font-weight: 600;">Thu phí</span>
                </div>
            </div>
        </div>
        '''
        m.get_root().html.add_child(folium.Element(legend_html))
        
        # FIT BOUNDS TỐT HƠN - Lấy tất cả các điểm quan trọng
        all_coords = []
        
        # Thêm điểm xuất phát và đích
        all_coords.append(start_coords)
        all_coords.append(end_coords)
        
        # Thêm tất cả điểm của routes (lấy mẫu để tránh quá nhiều điểm)
        for r in routes_data:
            all_coords.extend(r['coords'][::20])  # Lấy mỗi 20 điểm
        
        # Thêm các trạm sạc được chọn
        for r in routes_data:
            if r.get('is_best', False):  # Chỉ lấy route tốt nhất
                for stop in r['stops']:
                    all_coords.append([stop['station']['lat'], stop['station']['lon']])
        
        if all_coords:
            # Fit bounds với padding lớn hơn để không bị sát mép
            m.fit_bounds(all_coords, padding=[50, 50])
        
        print(f"⏱️ Tạo bản đồ: {time.time() - map_start:.1f}s")
        print(f"\n✅ TỔNG THỜI GIAN: {time.time() - total_start:.1f}s")
        print(f"{'='*60}\n")
        
        return jsonify({
            "map": m._repr_html_(),
            "routes": [{
                'name': r['name'], 
                'metrics': r['metrics'], 
                'charging_stops': r['stops'],
                'detail_chart': create_route_detail_chart(r) if r.get('is_best', False) else None,
                'is_best': r.get('is_best', False),
                'instructions': r['instructions'],
                'segments': r.get('segments', [])
            } for r in routes_data],
            "comparison_chart": create_comparison_chart(routes_data)
        })
        
    except Exception as e:
        print(f"\n❌ LỖI NGHIÊM TRỌNG: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Lỗi hệ thống: {str(e)}"}), 500

def find_highway_stations_near_route(route_coords, max_distance_km=3):
    """Tìm trạm cao tốc - TỐI ƯU"""
    if len(highway_stations) == 0:
        return []
    
    num_samples = min(10, len(route_coords))
    step = max(1, len(route_coords) // num_samples)
    sampled_coords = route_coords[::step]
    
    nearby_stations = []
    
    for station in highway_stations:
        station_pos = (station['lat'], station['lon'])
        min_distance = min(geodesic(station_pos, coord).km for coord in sampled_coords)
        
        if min_distance <= max_distance_km:
            nearby_stations.append({
                'station': station,
                'distance_to_route': round(min_distance, 2)
            })
    
    nearby_stations.sort(key=lambda x: x['distance_to_route'])
    return nearby_stations[:3]

def find_ferries_near_route(route_coords, max_distance_km=5):
    """Tìm phà - TỐI ƯU"""
    if len(ferries) == 0:
        return []
    
    num_samples = min(10, len(route_coords))
    step = max(1, len(route_coords) // num_samples)
    sampled_coords = route_coords[::step]
    
    nearby_ferries = []
    
    for ferry in ferries:
        ferry_pos = (ferry['lat'], ferry['lon'])
        min_distance = min(geodesic(ferry_pos, coord).km for coord in sampled_coords)
        
        if min_distance <= max_distance_km:
            nearby_ferries.append({
                'ferry': ferry,
                'distance_to_route': round(min_distance, 2)
            })
    
    nearby_ferries.sort(key=lambda x: x['distance_to_route'])
    return nearby_ferries[:3]

def find_toll_stations_near_route(route_coords, max_distance_km=2):
    """Tìm trạm thu phí - TỐI ƯU"""
    if len(toll_stations) == 0:
        return []
    
    num_samples = min(10, len(route_coords))
    step = max(1, len(route_coords) // num_samples)
    sampled_coords = route_coords[::step]
    
    nearby_stations = []
    
    for station in toll_stations:
        station_pos = (station['lat'], station['lon'])
        min_distance = min(geodesic(station_pos, coord).km for coord in sampled_coords)
        
        if min_distance <= max_distance_km:
            nearby_stations.append({
                'station': station,
                'distance_to_route': round(min_distance, 2)
            })
    nearby_stations.sort(key=lambda x: x['distance_to_route'])
    return nearby_stations[:3]

def find_charging_stations_near_route(route_coords, max_distance_km=5):
    """Tìm trạm sạc gần đường đi - TỐI ƯU CỰC MẠNH"""
    print(f"🔍 Tìm kiếm trạm sạc... (Tổng: {len(stations)} trạm)")
    
    if len(stations) == 0 or station_tree is None:
        print("⚠️ KHÔNG CÓ DỮ LIỆU TRẠM SẠC!")
        return []
    
    num_samples = min(15, len(route_coords))
    step = max(1, len(route_coords) // num_samples)
    sampled_coords = route_coords[::step]
    
    print(f"  📊 Lấy mẫu: {len(sampled_coords)} điểm từ {len(route_coords)} điểm gốc")
    
    nearby_stations_set = set()
    
    for coord in sampled_coords:
        try:
            distances, indices = station_tree.query(coord, k=5)
            for dist_deg, idx in zip(distances, indices):
                station = stations[idx]
                actual_distance = geodesic(coord, (station['lat'], station['lon'])).km
                
                if actual_distance <= max_distance_km:
                    nearby_stations_set.add((
                        idx,
                        round(actual_distance, 2)
                    ))
        except Exception as e:
            continue
    
    nearby_stations = [
        {
            'station': stations[idx],
            'distance_to_route': dist
        }
        for idx, dist in nearby_stations_set
    ]
    
    nearby_stations.sort(key=lambda x: x['distance_to_route'])
    print(f"✅ Tìm thấy {len(nearby_stations)} trạm sạc gần đường đi")
    
    return nearby_stations[:10]

if __name__ == '__main__':
    app.run(debug=True, port=5500, host='0.0.0.0')