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

# --- THƯ VIỆN TỐI ƯU ---
from scipy.spatial import cKDTree
from folium.plugins import MarkerCluster
# -----------------------

app = Flask(__name__)

# --- 1. LOAD DỮ LIỆU TỪ MONGODB & CACHE ---
print("Đang tải dữ liệu trạm sạc từ MongoDB...")
client = MongoClient('mongodb://localhost:27017/', maxPoolSize=1)
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
    print("⚠️ Chưa có dữ liệu trạm cao tốc trong MongoDB")
if len(toll_stations) == 0:
    print("⚠️ Chưa có dữ liệu trạm thu phí trong MongoDB")
    print("💡 Tạm thời sử dụng dữ liệu mẫu")
    # Dữ liệu mẫu trạm thu phí (nếu không có trong DB)
    toll_stations = []

client.close()

print(f"✅ Đã tải {len(stations)} trạm sạc")
print(f"✅ Đã tải {len(ferries)} bến phà")
print(f"✅ Đã tải {len(highway_stations)} trạm dừng cao tốc")
print(f"✅ Đã tải {len(toll_stations)} trạm thu phí")

# TẠO KD-TREE CHO TRẠM SẠC (TỐI ƯU TÌM KIẾM)
if len(stations) > 0:
    station_coords = np.array([[s['lat'], s['lon']] for s in stations])
    station_tree = cKDTree(station_coords)
    print("✅ Đã khởi tạo KD-Tree cho trạm sạc")
else:
    station_tree = None
    print("⚠️ Không có trạm sạc để tạo KD-Tree")

print("Hệ thống sẵn sàng!")
# ---------------------------------------------------------

# Geocoder
geolocator = Nominatim(user_agent="ev-app-hcm")

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

def create_map(center=[10.76, 106.66], zoom=12):
    """Tạo bản đồ cơ bản"""
    return folium.Map(location=center, zoom_start=zoom)

def add_stations_to_map(map_obj, nearby_ferries=None, nearby_highway_stations=None, nearby_toll_stations=None, show_all_ferries=True):
    """
    TỐI ƯU HIỂN THỊ BẢN ĐỒ:
    - nearby_ferries: phà gần đường đi
    - nearby_highway_stations: trạm cao tốc gần đường đi
    - nearby_toll_stations: trạm thu phí gần đường đi
    """
    # Cluster cho trạm sạc
    marker_cluster = MarkerCluster(
        name="Trạm sạc VinFast",
        options={
            'disableClusteringAtZoom': 14,
            'spiderfyOnMaxZoom': True
        }
    ).add_to(map_obj)

    for station in stations:
        icon_color = "green" if station.get('type') == 'AC' else "red"
        
        popup_info = f"""
            <div style="width: 200px; font-family: Arial;">
                <b style="color: #2980b9;">{station['name']}</b><br>
                <hr style="margin: 5px 0;">
                {station.get('address', 'N/A')}<br>
                Loại: <b>{station.get('type', 'DC Fast')}</b><br>
                Công suất: <b>{station.get('power', 150)} kW</b>
            </div>
        """
        
        folium.Marker(
            location=[station["lat"], station["lon"]],
            popup=popup_info,
            icon=folium.Icon(color=icon_color, icon="bolt", prefix='fa')
        ).add_to(marker_cluster)
    
    # XỬ LÝ HIỂN THỊ PHÀ
    if nearby_ferries:
        print(f"🚢 Hiển thị {len(nearby_ferries)} bến phà gần đường đi")
        
        ferry_cluster = MarkerCluster(
            name="Bến phà gần đường đi",
            options={
                'disableClusteringAtZoom': 10,  # Giảm từ 12 xuống 10
                'spiderfyOnMaxZoom': True,
                'maxClusterRadius': 60
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
    
    # XỬ LÝ HIỂN THỊ TRẠM DỪNG CAO TỐC - GIẢM ZOOM ĐỂ HIỆN SỚM HƠN
    if nearby_highway_stations:
        print(f"🛣️ Hiển thị {len(nearby_highway_stations)} trạm dừng cao tốc gần đường đi")
        
        highway_cluster = MarkerCluster(
            name="Trạm dừng cao tốc",
            options={
                'disableClusteringAtZoom': 9,  # GIẢM TỪ 12 → 9 để hiện sớm hơn
                'spiderfyOnMaxZoom': True,
                'maxClusterRadius': 50,
                'showCoverageOnHover': True
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
    
    # XỬ LÝ HIỂN THỊ TRẠM THU PHÍ - GIẢM ZOOM
    if nearby_toll_stations:
        print(f"💰 Hiển thị {len(nearby_toll_stations)} trạm thu phí gần đường đi")
        
        toll_cluster = MarkerCluster(
            name="Trạm thu phí",
            options={
                'disableClusteringAtZoom': 9,  # GIẢM TỪ 12 → 9
                'spiderfyOnMaxZoom': True,
                'maxClusterRadius': 50,
                'showCoverageOnHover': True
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
            
    elif show_all_ferries:
        # Hiển thị tất cả phà (khi không tìm đường)
        ferry_cluster = MarkerCluster(
            name="Bến phà",
            options={
                'disableClusteringAtZoom': 13,
                'spiderfyOnMaxZoom': True
            }
        ).add_to(map_obj)
        
        for ferry in ferries:
            vehicle_allowed = ferry.get('vehicle_allowed', 'yes')
            
            if vehicle_allowed == 'yes':
                icon_color = "blue"
                status_text = "✅ Phương tiện đi qua được"
                bg_color = "#e8f5e9"
            elif vehicle_allowed == 'no':
                icon_color = "gray"
                status_text = "❌ Không cho phương tiện qua"
                bg_color = "#ffebee"
            else:
                icon_color = "lightblue"
                status_text = f"ℹ️ Loại: {vehicle_allowed}"
                bg_color = "#e3f2fd"
            
            popup_info = f"""
                <div style="width: 240px; font-family: Arial;">
                    <b style="color: #3498db; font-size: 14px;">🚢 {ferry['name']}</b><br>
                    <hr style="margin: 5px 0;">
                    <div style="background: {bg_color}; padding: 8px; margin: 8px 0; border-radius: 5px;">
                        {status_text}
                    </div>
                </div>
            """
            
            folium.Marker(
                location=[ferry["lat"], ferry["lon"]],
                popup=popup_info,
                icon=folium.Icon(color=icon_color, icon="ship", prefix='fa'),
                tooltip=f"Phà: {ferry['name']}"
            ).add_to(ferry_cluster)

def get_route(start_coords, end_coords, options=None):
    """Lấy route từ OSRM API với fallback"""
    
    osrm_servers = [
        "http://router.project-osrm.org",
        "https://routing.openstreetmap.de/routed-car"
    ]
    
    params = {
        "overview": "full",
        "geometries": "polyline",
        "steps": "false",
        "alternatives": "true"
    }
    
    # Xử lý bộ lọc tránh phà
    excludes = []
    if options:
        if options.get('avoid_highways'): excludes.append('motorway')
        if options.get('avoid_tolls'): excludes.append('toll')
        if options.get('avoid_ferries'): excludes.append('ferry')  # Tránh phà
    
    for server_idx, base_url in enumerate(osrm_servers):
        try:
            url = f"{base_url}/route/v1/driving/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"
            
            if excludes:
                params['exclude'] = ','.join(excludes)
            
            print(f"Thử server {server_idx + 1}/{len(osrm_servers)}: {base_url}")
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == "Ok" and data.get("routes"):
                    routes = []
                    for route in data["routes"][:3]:
                        routes.append({
                            "geometry": route["geometry"],
                            "distance": route["distance"],
                            "duration": route["duration"],
                            "steps": route.get("legs", [{}])[0].get("steps", [])
                        })
                    print(f"✅ Nhận được {len(routes)} route(s) từ {base_url}")
                    return routes
            
            # Nếu thất bại với exclude, thử lại không có exclude
            if 'exclude' in params:
                params_no_exclude = {k: v for k, v in params.items() if k != 'exclude'}
                response = requests.get(url, params=params_no_exclude, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        routes = []
                        for route in data["routes"][:3]:
                            routes.append({
                                "geometry": route["geometry"],
                                "distance": route["distance"],
                                "duration": route["duration"],
                                "steps": route.get("legs", [{}])[0].get("steps", [])
                            })
                        print(f"✅ Nhận được {len(routes)} route(s) (không có bộ lọc)")
                        return routes
                        
        except requests.exceptions.Timeout:
            print(f"⏱️ Server {base_url} timeout")
            continue
        except Exception as e:
            print(f"❌ Server {base_url} lỗi: {e}")
            continue
    
    print("❌ Tất cả OSRM servers đều thất bại")
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
    
    # Tính nhanh: Chia đoạn đường thành các checkpoint
    num_checkpoints = max(3, int(route_distance / 100))  # Mỗi 100km 1 checkpoint
    checkpoint_step = max(1, len(route_coords) // num_checkpoints)
    checkpoints = [start_coords]
    for i in range(checkpoint_step, len(route_coords), checkpoint_step):
        checkpoints.append(route_coords[i])
    checkpoints.append(end_coords)
    
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
            
            # SẠC ĐẦY 100% MỖI KHI DỪNG - FIXED!
            target_battery = 100
            
            charge_time = calculate_charge_time(battery_on_arrival, target_battery, station.get('power', 150), config)
            
            charging_stops.append({
                'station': station,
                'arrival_battery': round(battery_on_arrival, 1),
                'start_charge': round(battery_on_arrival, 1),
                'end_charge': 100,  # Luôn 100%
                'charge_time': round(charge_time, 1),
                'distance_from_start': round(total_distance + reachable[0]['distance'], 1)
            })
            
            # CẬP NHẬT PIN SAU KHI SẠC = 100%
            battery_percent = 100
            total_distance += reachable[0]['distance']
            
            # Tính lại quãng đường từ trạm sạc đến checkpoint tiếp theo
            segment_dist = geodesic((station['lat'], station['lon']), checkpoints[i+1]).km
            battery_needed = calculate_battery_consumption(segment_dist, config)
        
        # Trừ pin cho đoạn đường
        battery_percent -= battery_needed
        total_distance += segment_dist
        
        # Đảm bảo pin không âm
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

def add_route_to_map(map_obj, route_coords, charging_stops, color="blue", label=""):
    """Thêm route và highlight các trạm sạc đã chọn"""
    # VẼ ĐƯỜNG ĐI - DÙNG TẤT CẢ ĐIỂM (step=1) ĐỂ ĐƯỜNG CONG CHI TIẾT
    folium.PolyLine(route_coords, color=color, weight=6, opacity=0.8, 
                    popup=label, smooth_factor=1.0).add_to(map_obj)
    
    # Điểm xuất phát
    folium.Marker(
        route_coords[0], 
        icon=folium.Icon(color="green", icon="play", prefix='fa'),
        popup=folium.Popup("<b style='font-size: 14px;'>Điểm xuất phát</b>", max_width=200)
    ).add_to(map_obj)
    
    # Điểm đến
    folium.Marker(
        route_coords[-1], 
        icon=folium.Icon(color="red", icon="flag-checkered", prefix='fa'),
        popup=folium.Popup("<b style='font-size: 14px;'> Điểm đến</b>", max_width=200)
    ).add_to(map_obj)
    
    # Highlight trạm sạc đã chọn với icon to và rõ ràng
    for i, stop in enumerate(charging_stops, 1):
        s = stop['station']
        
        popup_content = f"""
            <div style='min-width: 250px; font-family: Inter, Arial; padding: 5px;'>
                <h4 style='margin: 0 0 10px 0; color: #f97316; border-bottom: 2px solid #f97316; padding-bottom: 5px;'>
                     TRẠM SẠC #{i}
                </h4>
                <div style='line-height: 1.8;'>
                    <b style='color: #1e293b;'>{s['name']}</b><br>
                     {s.get('address', 'N/A')}<br>
                    <hr style='margin: 8px 0; border: none; border-top: 1px solid #e2e8f0;'>
                    <div style='background: #fef3c7; padding: 8px; border-radius: 6px; margin: 5px 0;'>
                        Công suất: <b style='color: #d97706;'>{s.get('power', 150)} kW</b><br>
                        Pin đến: <b style='color: #dc2626;'>{stop['arrival_battery']}%</b><br>
                        Sạc đến: <b style='color: #16a34a;'>{stop['end_charge']}%</b><br>
                        Thời gian: <b style='color: #2563eb;'>{stop['charge_time']} phút</b>
                    </div>
                    <small style='color: #64748b;'>Quãng đường đã đi: {stop['distance_from_start']} km</small>
                </div>
            </div>
        """
        
        # Icon lớn và nổi bật cho trạm sạc đã chọn
        folium.Marker(
            [s['lat'], s['lon']],
            icon=folium.Icon(
                color="orange", 
                icon="bolt", 
                prefix='fa',
                icon_color='white'
            ),
            popup=folium.Popup(popup_content, max_width=300),
            tooltip=f"Trạm sạc #{i}: {s['name']}"
        ).add_to(map_obj)
        
        # Vẽ circle để highlight khu vực
        folium.Circle(
            location=[s['lat'], s['lon']],
            radius=500,  # 500m
            color='#f97316',
            fill=True,
            fillColor='#f97316',
            fillOpacity=0.1,
            weight=2,
            popup=f"Vùng phủ sóng trạm {i}"
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
    
    # Thêm giá trị trên các cột với font lớn hơn
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
    
    # BỎ THÔNG TIN THỜI GIAN VÀ KM Ở DƯỚI
    # (Xóa hoàn toàn đoạn code hiển thị thông tin dưới chart)
    
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
        print(f"Từ: {start_addr}")
        print(f"Đến: {end_addr}")
        print(f"Pin ban đầu: {data.get('start_battery', 100)}%")
        print(f"Tránh phà: {'Có' if data.get('avoid_ferries') else 'Không'}")
        
        start_loc = geolocator.geocode(start_addr + ", Việt Nam", timeout=10)
        end_loc = geolocator.geocode(end_addr + ", Việt Nam", timeout=10)
        
        if not start_loc or not end_loc: 
            return jsonify({"error": "Không tìm thấy địa chỉ. Vui lòng kiểm tra lại tên địa điểm."}), 400
        
        start_coords = (start_loc.latitude, start_loc.longitude)
        end_coords = (end_loc.latitude, end_loc.longitude)
        print(f"✅ Geocode thành công")
        print(f"   Start: {start_coords}")
        print(f"   End: {end_coords}")
        
        direct_distance = geodesic(start_coords, end_coords).km
        print(f"📏 Khoảng cách đường chim bay: {direct_distance:.1f} km")
        
        if direct_distance < 1:
            return jsonify({"error": "Hai điểm quá gần nhau (< 1km). Không cần tìm đường."}), 400
        
        print(f"\n🌐 Đang kết nối OSRM server...")
        base_routes = get_route(start_coords, end_coords, data)
        
        if not base_routes: 
            return jsonify({
                "error": "Không thể kết nối đến OSRM server. Vui lòng:\n"
                        "1. Kiểm tra kết nối Internet\n"
                        "2. Thử lại sau vài phút\n"
                        "3. OSRM public server có thể đang bảo trì"
            }), 400
        
        print(f"✅ Nhận được {len(base_routes)} route(s) từ OSRM")
        
        routes_data = []
        route_names = ['Lộ trình tối ưu', 'Lộ trình thay thế 1', 'Lộ trình thay thế 2']
        route_colors = ['#3b82f6', '#94a3b8', '#9ca3af']
        
        # XỬ LÝ TẤT CẢ ROUTES
        for idx, base_route in enumerate(base_routes[:3]):
            try:
                route_coords = polyline.decode(base_route['geometry'])
                route_distance_km = base_route['distance'] / 1000
                print(f"\n📍 Route {idx+1}: {route_distance_km:.1f}km, {len(route_coords)} điểm")
                
                result, err = a_star_ev_routing(
                    start_coords, end_coords, route_coords, 
                    float(data.get('start_battery', 100)), EV_CONFIG
                )
                
                if result:
                    routes_data.append({
                        'name': route_names[idx] if idx < len(route_names) else f'Lộ trình {idx+1}',
                        'route': base_route, 
                        'coords': route_coords,
                        'stops': result['charging_stops'], 
                        'metrics': result, 
                        'feasible': True,
                        'color': route_colors[idx] if idx < len(route_colors) else '#9ca3af',
                        'is_best': False
                    })
                    print(f"   ✅ Khả thi - {result['num_charges']} lần sạc, {result['total_time']:.0f} phút")
                else:
                    print(f"   ❌ Không khả thi: {err}")
            except Exception as e:
                print(f"   ❌ Lỗi xử lý: {e}")
                continue
        
        if not routes_data: 
            return jsonify({
                "error": "Không có lộ trình khả thi cho xe điện.\n\n"
                        "💡 Đề xuất:\n"
                        f"• Tăng pin ban đầu (hiện tại: {data.get('start_battery', 100)}%)\n"
                        f"• Giảm mức tiêu thụ (hiện tại: {data.get('consumption', 16.3)} kWh/100km)\n"
                        f"• Khoảng cách: {direct_distance:.1f}km có thể quá xa cho mức pin hiện tại"
            }), 400
        
        print(f"\n✅ Có {len(routes_data)} lộ trình khả thi")
        
        # SẮP XẾP VÀ ĐÁNH DẤU ROUTE TỐT NHẤT
        routes_data.sort(key=lambda x: x['metrics']['total_time'])
        routes_data[0]['name'] = 'Lộ trình tối ưu'
        routes_data[0]['color'] = '#3b82f6'
        routes_data[0]['is_best'] = True
        
        for idx in range(1, len(routes_data)):
            routes_data[idx]['name'] = f'Lộ trình thay thế {idx}'
            routes_data[idx]['color'] = '#94a3b8' if idx == 1 else '#9ca3af'
        
        # Tìm phà, trạm cao tốc và trạm thu phí gần đường đi
        avoid_ferries = data.get('avoid_ferries', False)
        avoid_highways = data.get('avoid_highways', False)
        avoid_tolls = data.get('avoid_tolls', False)
        nearby_ferries = []
        nearby_highway_stations = []
        nearby_toll_stations = []
        
        if routes_data:
            best_route_coords = routes_data[0]['coords']
            
            # Tìm phà nếu chọn "Tránh phà"
            if avoid_ferries:
                nearby_ferries = find_ferries_near_route(best_route_coords, max_distance_km=5)
                print(f"🚢 Tìm thấy {len(nearby_ferries)} bến phà gần đường đi (trong bán kính 5km)")
            
            # Tìm trạm cao tốc nếu KHÔNG tránh cao tốc (tức là đi cao tốc)
            if not avoid_highways:
                nearby_highway_stations = find_highway_stations_near_route(best_route_coords, max_distance_km=3)
                print(f"🛣️ Tìm thấy {len(nearby_highway_stations)} trạm dừng cao tốc gần đường đi (trong bán kính 3km)")
            
            # Tìm trạm thu phí nếu chọn "Tránh trạm thu phí"
            if avoid_tolls:
                nearby_toll_stations = find_toll_stations_near_route(best_route_coords, max_distance_km=2)
                print(f"💰 Tìm thấy {len(nearby_toll_stations)} trạm thu phí gần đường đi (trong bán kính 2km)")
        
        # TẠO BẢN ĐỒ
        center_lat = (start_coords[0] + end_coords[0]) / 2
        center_lon = (start_coords[1] + end_coords[1]) / 2
        m = folium.Map(
            location=[center_lat, center_lon], 
            zoom_start=8,
            tiles='OpenStreetMap',
            prefer_canvas=True
        )
        
        # Thêm trạm sạc, phà, trạm cao tốc và trạm thu phí
        add_stations_to_map(
            m, 
            nearby_ferries=nearby_ferries if avoid_ferries else None,
            nearby_highway_stations=nearby_highway_stations if not avoid_highways else None,
            nearby_toll_stations=nearby_toll_stations if avoid_tolls else None
        )
        
        for r in reversed(routes_data):
            add_route_to_map(m, r['coords'], r['stops'], r['color'], r['name'])
        
        legend_html = '''
        <div style="position: fixed; 
                    bottom: 50px; right: 50px; 
                    background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
                    padding: 20px; 
                    border-radius: 16px;
                    box-shadow: 0 10px 25px rgba(0,0,0,0.15);
                    z-index: 9999;
                    font-family: 'Inter', Arial, sans-serif;
                    font-size: 13px;
                    min-width: 240px;
                    border: 2px solid #e2e8f0;">
            <h4 style="margin: 0 0 15px 0; 
                       font-size: 16px; 
                       color: #1e293b;
                       border-bottom: 2px solid #6366f1;
                       padding-bottom: 10px;">
                📍 Chú thích
            </h4>
            <div style="line-height: 2.2;">
                <div><i class="fa fa-play" style="color: green; width: 20px;"></i> Điểm xuất phát</div>
                <div><i class="fa fa-flag-checkered" style="color: red; width: 20px;"></i> Điểm đến</div>
                <div style="height: 3px; width: 40px; background: #3b82f6; display: inline-block; vertical-align: middle;"></div> <b>Lộ trình tối ưu</b>
                <div style="height: 3px; width: 40px; background: #94a3b8; display: inline-block; vertical-align: middle;"></div> Lộ trình thay thế
                <div><i class="fa fa-bolt" style="color: orange; width: 20px;"></i> <b>Trạm sạc đã chọn</b></div>
                <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid #e2e8f0;">
                    <div><i class="fa fa-ship" style="color: blue; width: 20px;"></i> Bến phà</div>
                    <div><i class="fa fa-gas-pump" style="color: orange; width: 20px;"></i> Trạm dừng cao tốc</div>
                    <div><i class="fa fa-money-bill" style="color: red; width: 20px;"></i> Trạm thu phí</div>
                </div>
            </div>
            <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #64748b;">
                💡 Các điểm gần đường đi sẽ hiện theo tùy chọn
            </div>
        </div>
        '''
        m.get_root().html.add_child(folium.Element(legend_html))
        
        all_coords = []
        for r in routes_data:
            all_coords.extend(r['coords'])
        if all_coords:
            m.fit_bounds(all_coords, padding=[50, 50])
        
        print(f"\n{'='*60}")
        print(f"✅ HOÀN THÀNH - Trả về {len(routes_data)} lộ trình")
        print(f"{'='*60}\n")
            
        return jsonify({
            "map": m._repr_html_(),
            "routes": [{
                'name': r['name'], 
                'metrics': r['metrics'], 
                'charging_stops': r['stops'],
                'detail_chart': create_route_detail_chart(r),
                'is_best': r.get('is_best', False)
            } for r in routes_data],
            "comparison_chart": create_comparison_chart(routes_data)
        })

    except Exception as e:
        print(f"\n❌ LỖI NGHIÊM TRỌNG: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Lỗi hệ thống: {str(e)}"}), 500

def find_highway_stations_near_route(route_coords, max_distance_km=3):
    """Tìm trạm dừng cao tốc gần đường đi"""
    print(f"🔍 Tìm kiếm trạm cao tốc... (Tổng: {len(highway_stations)} trạm)")
    
    if len(highway_stations) == 0:
        print("⚠️ KHÔNG CÓ DỮ LIỆU TRẠM CAO TỐC TRONG HỆ THỐNG!")
        return []
    
    nearby_stations = []
    sampled_coords = route_coords[::20] if len(route_coords) > 20 else route_coords
    
    for station in highway_stations:
        station_pos = (station['lat'], station['lon'])
        min_distance = float('inf')
        
        for coord in sampled_coords:
            distance = geodesic(station_pos, coord).km
            if distance < min_distance:
                min_distance = distance
        
        if min_distance <= max_distance_km:
            nearby_stations.append({
                'station': station,
                'distance_to_route': round(min_distance, 2)
            })
    
    nearby_stations.sort(key=lambda x: x['distance_to_route'])
    print(f"✅ Tìm thấy {len(nearby_stations)} trạm cao tốc gần đường đi")
    
    return nearby_stations

def find_ferries_near_route(route_coords, max_distance_km=5):
    """
    Tìm các bến phà nằm gần đường đi
    route_coords: list các tọa độ [(lat, lon), ...]
    max_distance_km: khoảng cách tối đa (mặc định 5km)
    """
    nearby_ferries = []
    
    # Lấy mẫu route để tăng tốc (mỗi 20 điểm lấy 1)
    sampled_coords = route_coords[::20] if len(route_coords) > 20 else route_coords
    
    for ferry in ferries:
        ferry_pos = (ferry['lat'], ferry['lon'])
        min_distance = float('inf')
        
        # Tính khoảng cách ngắn nhất từ phà đến đường đi
        for coord in sampled_coords:
            distance = geodesic(ferry_pos, coord).km
            if distance < min_distance:
                min_distance = distance
        
        # Nếu phà nằm trong bán kính
        if min_distance <= max_distance_km:
            nearby_ferries.append({
                'ferry': ferry,
                'distance_to_route': round(min_distance, 2)
            })
    
    # Sắp xếp theo khoảng cách
    nearby_ferries.sort(key=lambda x: x['distance_to_route'])
    
    return nearby_ferries

def find_toll_stations_near_route(route_coords, max_distance_km=2):
    """Tìm trạm thu phí gần đường đi"""
    print(f"🔍 Tìm kiếm trạm thu phí... (Tổng: {len(toll_stations)} trạm)")
    
    if len(toll_stations) == 0:
        print("⚠️ KHÔNG CÓ DỮ LIỆU TRẠM THU PHÍ TRONG HỆ THỐNG!")
        return []
    
    nearby_stations = []
    sampled_coords = route_coords[::20] if len(route_coords) > 20 else route_coords
    
    for station in toll_stations:
        station_pos = (station['lat'], station['lon'])
        min_distance = float('inf')
        
        for coord in sampled_coords:
            distance = geodesic(station_pos, coord).km
            if distance < min_distance:
                min_distance = distance
        
        if min_distance <= max_distance_km:
            nearby_stations.append({
                'station': station,
                'distance_to_route': round(min_distance, 2)
            })
    
    nearby_stations.sort(key=lambda x: x['distance_to_route'])
    print(f"✅ Tìm thấy {len(nearby_stations)} trạm thu phí gần đường đi")
    
    return nearby_stations

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')