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
import hashlib
import os
from dotenv import load_dotenv
from functools import lru_cache

# --- THƯ VIỆN TỐI ƯU ---
from scipy.spatial import cKDTree
from folium.plugins import MarkerCluster
# -----------------------

# ==============================================================================
#  CACHING CHO OSRM REQUESTS
# ==============================================================================
ROUTE_CACHE = {}  # Cache cho các request OSRM
CACHE_TTL = 3600  # Cache 1 giờ

# ==============================================================================
#  CẤU HÌNH GOONG API (API chính cho Việt Nam)
# ==============================================================================
GOONG_API_KEY = "P7tEN9p2VUxhqxFQJ7fWdaPzDEbs5YfnRoXw0fxm"  # Routing API
GOONG_MAPTILES_KEY = "3kieVmAErykLSTEnl574OCGXiv9qUlo1QdK1lqGU"  # Map Tiles API
GOONG_DIRECTION_URL = "https://rsapi.goong.io/Direction"

# ==============================================================================
#  CẤU HÌNH MAPBOX BACKUP (Fallback khi Goong thất bại)
# ==============================================================================
MAPBOX_ACCESS_TOKEN = "pk.eyJ1IjoidmFuaHV5MjAwNSIsImEiOiJjbWozMmZlYnAwMGR0M2ZxOThwYm0xZGE3In0.GDK-bu28Jp-7DOCYKryidg"

def get_cache_key(start_coords, end_coords, options=None):
    """Tạo key cache từ tọa độ (làm tròn 5 chữ số)"""
    key_str = f"{round(start_coords[0], 5)},{round(start_coords[1], 5)}-{round(end_coords[0], 5)},{round(end_coords[1], 5)}"
    if options:
        key_str += f"-{json.dumps(options, sort_keys=True)}"
    return hashlib.md5(key_str.encode()).hexdigest()

# ==============================================================================
#  CẤU HÌNH GIÁ MẶC ĐỊNH
# ==============================================================================
DEFAULT_PRICE_PER_KWH = 3500  # VND/kWh
DEFAULT_TOLL_FEE = 35000      # VND/trạm (mức trung bình)

# ==============================================================================
#  GHI CHÚ: Đã loại bỏ VIETNAM_ANCHORS và find_nearest_anchor
#  Goong API được tối ưu hóa cho hạ tầng giao thông Việt Nam,
#  không cần logic "chia nhỏ lộ trình" (chunking) và các điểm neo.
# ==============================================================================

app = Flask(__name__)

load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')

# --- 1. LOAD DỮ LIỆU TỪ MONGODB & CACHE ---
print("Đang tải dữ liệu trạm sạc từ MongoDB...")
try:
    client = MongoClient(MONGO_URI, maxPoolSize=1, serverSelectionTimeoutMS=2000)
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
    Chuyển đổi các bước đi (steps) từ OSRM/Goong sang hướng dẫn tiếng Việt chi tiết.
    
    Hỗ trợ cả 2 format:
    - OSRM/Mapbox: Dùng maneuver.type và maneuver.modifier
    - Goong: Dùng html_instructions (ưu tiên nếu có)
    
    BỔ SUNG: Thêm thời gian di chuyển cho mỗi đoạn
    """
    import re
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
        html_instructions = step.get('html_instructions', '')  # GOONG: HTML instructions
        
        # === ƯU TIÊN GOONG html_instructions NẾU CÓ ===
        if html_instructions:
            # Loại bỏ HTML tags để lấy text thuần
            clean_text = re.sub(r'<[^>]+>', '', html_instructions)
            full_text = clean_text.strip()
            
            # Nếu không có name, thử trích xuất từ html_instructions
            if not name:
                bold_match = re.search(r'<b>([^<]+)</b>', html_instructions)
                if bold_match:
                    name = bold_match.group(1).strip()
        else:
            # === LOGIC GỐC CHO OSRM/MAPBOX ===
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

def create_infeasible_map(start_coords, end_coords, traveled_coords, remaining_coords, failure_point, nearest_station=None):
    """
    Tạo bản đồ hiển thị lộ trình bị đứt gãy do hết pin
    """
    # 1. Tạo Map Center (lấy trung điểm của đoạn đã đi được)
    if traveled_coords and len(traveled_coords) > 0:
        mid_idx = len(traveled_coords) // 2
        center = traveled_coords[mid_idx]
    else:
        center = start_coords
        
    m = create_map(center=center, zoom=8)
    
    # 2. Vẽ đoạn đường đi được (Màu Đỏ - Solid)
    if traveled_coords and len(traveled_coords) > 0:
        folium.PolyLine(
            traveled_coords,
            color='#ef4444',  # Đỏ
            weight=5,
            opacity=0.8,
            tooltip="Quãng đường đi được"
        ).add_to(m)
        
    # 3. Vẽ đoạn còn lại (Màu Xám - Dashed)
    if remaining_coords and len(remaining_coords) > 0:
        folium.PolyLine(
            remaining_coords,
            color='#94a3b8',  # Xám
            weight=4,
            opacity=0.6,
            dash_array='10, 10',  # Nét đứt
            tooltip="Quãng đường còn lại (Không thể tới)"
        ).add_to(m)

    # 4. Marker Xuất phát
    folium.Marker(
        start_coords,
        icon=folium.Icon(color="green", icon="play", prefix='fa'),
        popup="Xuất phát"
    ).add_to(m)

    # 5. Marker Đích (Mờ đi hoặc màu xám)
    folium.Marker(
        end_coords,
        icon=folium.Icon(color="gray", icon="flag-checkered", prefix='fa'),
        popup="Đích đến (Không thể tới)"
    ).add_to(m)

    # 6. Marker ĐIỂM CHẾT (Quan trọng nhất)
    if failure_point:
        fail_html = """
        <div style="
            background: #dc2626; color: white; 
            padding: 5px 10px; border-radius: 20px; 
            font-weight: bold; font-size: 12px;
            box-shadow: 0 3px 6px rgba(0,0,0,0.4);
            border: 2px solid white; white-space: nowrap;
        ">
            🪫 HẾT PIN
        </div>
        """
        folium.Marker(
            [failure_point[0], failure_point[1]],
            icon=folium.DivIcon(html=fail_html, icon_size=(80, 30), icon_anchor=(40, 15)),
            popup="Xe dừng tại đây do hết năng lượng",
            zIndexOffset=1000
        ).add_to(m)

    # 7. Vẽ trạm sạc cứu hộ (nếu có)
    if nearest_station:
        station_loc = [nearest_station['lat'], nearest_station['lon']]
        
        # Đường nối từ điểm chết đến trạm
        folium.PolyLine(
            [failure_point, station_loc],
            color='#f59e0b',  # Vàng cam
            weight=3,
            dash_array='5, 5',
            opacity=0.8
        ).add_to(m)
        
        # Marker trạm
        folium.Marker(
            station_loc,
            icon=folium.Icon(color="orange", icon="bolt", prefix='fa'),
            tooltip=f"Trạm gần nhất: {nearest_station.get('name', 'Unknown')}"
        ).add_to(m)
        
    # Fit bounds
    all_points = (traveled_coords or []) + (remaining_coords or [])
    if all_points and len(all_points) > 0:
        m.fit_bounds(all_points, padding=[50, 50])
        
    return m._repr_html_()

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
            
# ==============================================================================
#  HÀM LẤY ROUTE TỪ GOONG API (API CHÍNH)
# ==============================================================================
def get_route_from_goong(start_coords, end_coords, vehicle='car'):
    """
    Lấy route từ Goong Direction API - Tối ưu cho Việt Nam.
    
    Goong API được thiết kế riêng cho hạ tầng giao thông Việt Nam,
    tự động xử lý các đường nội địa mà không cần logic chunking/anchors.
    
    Args:
        start_coords: Tuple (lat, lon) điểm bắt đầu
        end_coords: Tuple (lat, lon) điểm kết thúc
        vehicle: Phương tiện ('car', 'bike', 'taxi')
        
    Returns:
        list: Danh sách routes theo format chuẩn OSRM hoặc [] nếu thất bại
    """
    try:
        # Goong API yêu cầu format: lat,lon (Vĩ độ, Kinh độ)
        origin = f"{start_coords[0]},{start_coords[1]}"
        destination = f"{end_coords[0]},{end_coords[1]}"
        
        params = {
            "origin": origin,
            "destination": destination,
            "vehicle": vehicle,
            "api_key": GOONG_API_KEY
        }
        
        print(f"🗺️ Đang gọi Goong Direction API...")
        print(f"   📍 Origin: {origin}")
        print(f"   📍 Destination: {destination}")
        
        response = requests.get(GOONG_DIRECTION_URL, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("status") == "OK" and data.get("routes"):
                print(f"✅ Goong API trả về {len(data['routes'])} route(s)")
                
                # === ADAPTER: Chuyển đổi Goong Response → OSRM Format ===
                standardized_routes = []
                
                for goong_route in data["routes"]:
                    # Lấy geometry từ overview_polyline
                    geometry = goong_route.get("overview_polyline", {}).get("points", "")
                    
                    # Lấy thông tin từ legs (Goong chỉ có 1 leg cho 2 điểm)
                    legs = goong_route.get("legs", [])
                    if not legs:
                        continue
                    
                    leg = legs[0]
                    distance = leg.get("distance", {}).get("value", 0)  # meters
                    duration = leg.get("duration", {}).get("value", 0)  # seconds
                    
                    # === CHUYỂN ĐỔI STEPS SANG FORMAT OSRM ===
                    osrm_steps = []
                    goong_steps = leg.get("steps", [])
                    
                    for step in goong_steps:
                        # Trích xuất thông tin từ Goong step
                        step_distance = step.get("distance", {}).get("value", 0)
                        step_duration = step.get("duration", {}).get("value", 0)
                        step_geometry = step.get("polyline", {}).get("points", "")
                        
                        # Lấy tên đường từ html_instructions
                        html_instructions = step.get("html_instructions", "")
                        # Xử lý maneuver từ Goong
                        maneuver_type, modifier = parse_goong_maneuver(step.get("maneuver", ""), html_instructions)
                        
                        # Trích xuất tên đường từ html_instructions
                        road_name = extract_road_name_from_instructions(html_instructions)
                        
                        osrm_steps.append({
                            "geometry": step_geometry,
                            "distance": step_distance,
                            "duration": step_duration,
                            "name": road_name,
                            "maneuver": {
                                "type": maneuver_type,
                                "modifier": modifier
                            },
                            "html_instructions": html_instructions  # Giữ lại để backup
                        })
                    
                    standardized_routes.append({
                        "geometry": geometry,
                        "distance": distance,
                        "duration": duration,
                        "steps": osrm_steps
                    })
                
                return standardized_routes
            else:
                error_msg = data.get("error_message", data.get("status", "Unknown error"))
                print(f"⚠️ Goong API error: {error_msg}")
                return []
                
        elif response.status_code == 400:
            print(f"❌ Goong Bad Request (400): {response.text[:200]}")
            return []
        elif response.status_code == 429:
            print(f"⏳ Goong Rate Limited (429) - Vượt quá giới hạn request")
            return []
        else:
            print(f"❌ Goong Error {response.status_code}: {response.text[:100]}")
            return []
            
    except requests.exceptions.Timeout:
        print("⏱️ Goong request timeout (30s)")
        return []
    except requests.exceptions.ConnectionError as e:
        print(f"🔌 Lỗi kết nối Goong: {e}")
        return []
    except Exception as e:
        print(f"❌ Lỗi không mong đợi khi gọi Goong: {e}")
        return []


def parse_goong_maneuver(maneuver_str, html_instructions=""):
    """
    Chuyển đổi maneuver từ Goong sang format OSRM.
    
    Goong maneuver: "turn-left", "turn-right", "straight", "roundabout-left", etc.
    OSRM format: type="turn", modifier="left"
    
    Args:
        maneuver_str: Chuỗi maneuver từ Goong
        html_instructions: HTML instructions để hỗ trợ phân tích
        
    Returns:
        tuple: (type, modifier)
    """
    if not maneuver_str:
        # Phân tích từ html_instructions nếu không có maneuver
        html_lower = html_instructions.lower()
        if "rẽ trái" in html_lower or "turn left" in html_lower:
            return "turn", "left"
        elif "rẽ phải" in html_lower or "turn right" in html_lower:
            return "turn", "right"
        elif "đi thẳng" in html_lower or "continue" in html_lower:
            return "continue", "straight"
        elif "quay đầu" in html_lower or "u-turn" in html_lower:
            return "turn", "uturn"
        elif "vòng xuyến" in html_lower or "roundabout" in html_lower:
            return "roundabout", None
        return "continue", "straight"
    
    maneuver_lower = maneuver_str.lower()
    
    # Mapping Goong maneuver -> OSRM format
    maneuver_map = {
        "turn-left": ("turn", "left"),
        "turn-right": ("turn", "right"),
        "turn-slight-left": ("turn", "slight left"),
        "turn-slight-right": ("turn", "slight right"),
        "turn-sharp-left": ("turn", "sharp left"),
        "turn-sharp-right": ("turn", "sharp right"),
        "straight": ("continue", "straight"),
        "keep-left": ("fork", "left"),
        "keep-right": ("fork", "right"),
        "ramp-left": ("on ramp", "left"),
        "ramp-right": ("on ramp", "right"),
        "merge": ("merge", None),
        "roundabout-left": ("roundabout", "left"),
        "roundabout-right": ("roundabout", "right"),
        "uturn-left": ("turn", "uturn"),
        "uturn-right": ("turn", "uturn"),
        "ferry": ("ferry", None),
        "depart": ("depart", None),
        "arrive": ("arrive", None)
    }
    
    if maneuver_lower in maneuver_map:
        return maneuver_map[maneuver_lower]
    
    # Fallback: phân tích từ chuỗi
    if "left" in maneuver_lower:
        return "turn", "left"
    elif "right" in maneuver_lower:
        return "turn", "right"
    elif "straight" in maneuver_lower:
        return "continue", "straight"
    
    return "continue", "straight"


def extract_road_name_from_instructions(html_instructions):
    """
    Trích xuất tên đường từ html_instructions của Goong.
    
    Ví dụ: "Rẽ trái vào <b>Nguyễn Huệ</b>" -> "Nguyễn Huệ"
    
    Args:
        html_instructions: Chuỗi HTML instructions từ Goong
        
    Returns:
        str: Tên đường hoặc "Đường không tên"
    """
    import re
    
    if not html_instructions:
        return "Đường không tên"
    
    # Tìm text trong thẻ <b>
    bold_match = re.search(r'<b>([^<]+)</b>', html_instructions)
    if bold_match:
        return bold_match.group(1).strip()
    
    # Tìm text sau "vào" hoặc "vào đường"
    vao_match = re.search(r'vào\s+(?:đường\s+)?([^<,\.]+)', html_instructions, re.IGNORECASE)
    if vao_match:
        return vao_match.group(1).strip()
    
    # Loại bỏ HTML tags và trả về text thuần
    clean_text = re.sub(r'<[^>]+>', '', html_instructions)
    
    # Nếu text ngắn, trả về luôn
    if len(clean_text) < 50:
        return clean_text.strip() or "Đường không tên"
    
    return "Đường không tên"


def get_route(start_coords, end_coords, options=None):
    """
    Lấy route với chiến lược Goong-first, fallback sang Mapbox.
    
    Luồng xử lý:
    1. Kiểm tra cache
    2. Gọi Goong API (tối ưu cho Việt Nam)
    3. Nếu Goong thất bại → Fallback sang Mapbox
    4. Lưu cache kết quả
    
    Đã loại bỏ logic chunking/anchors vì Goong xử lý native.
    """
    
    # === 1. VALIDATE & LÀM TRÒN TỌA ĐỘ ===
    start_coords = (round(start_coords[0], 5), round(start_coords[1], 5))
    end_coords = (round(end_coords[0], 5), round(end_coords[1], 5))
    
    # === 2. KIỂM TRA CACHE ===
    cache_key = get_cache_key(start_coords, end_coords, options)
    if cache_key in ROUTE_CACHE:
        cache_entry = ROUTE_CACHE[cache_key]
        if time.time() - cache_entry['time'] < CACHE_TTL:
            print(f"💾 Cache hit! Sử dụng kết quả đã lưu")
            return cache_entry['data'], None
    
    # === 3. TÍNH KHOẢNG CÁCH ===
    distance_km = geodesic(start_coords, end_coords).km
    print(f"📏 Khoảng cách ước tính: {distance_km:.0f}km")
    
    # === 4. GỌI GOONG API (API CHÍNH) ===
    print(f"\n🗺️ === SỬ DỤNG GOONG API (Tối ưu cho Việt Nam) ===")
    routes = get_route_from_goong(start_coords, end_coords, vehicle='car')
    
    if routes:
        print(f"✅ Goong thành công! Nhận được {len(routes)} route(s)")
        # Lưu cache
        ROUTE_CACHE[cache_key] = {'data': routes, 'time': time.time()}
        return routes, None
    
    # === 5. FALLBACK SANG MAPBOX ===
    print(f"\n⚠️ Goong thất bại, chuyển sang Mapbox backup...")
    mapbox_routes = get_route_from_mapbox(start_coords, end_coords)
    
    if mapbox_routes:
        print(f"✅ Mapbox backup thành công!")
        # Lưu cache
        ROUTE_CACHE[cache_key] = {'data': mapbox_routes, 'time': time.time()}
        return mapbox_routes, None
    
    # === 6. TẤT CẢ ĐỀU THẤT BẠI ===
    print("=" * 50)
    print("❌ THẤT BẠI: Không thể lấy được lộ trình từ cả Goong và Mapbox")
    print(f"📍 Từ: {start_coords}")
    print(f"📍 Đến: {end_coords}")
    print(f"📏 Khoảng cách: {distance_km:.1f} km")
    print("=" * 50)
    
    return [], "Không thể kết nối đến server định tuyến (Goong/Mapbox)"

def get_route_from_mapbox(start_coords, end_coords):
    """
    Hàm gọi Mapbox API khi OSRM thất bại (Backup Plan).
    Trả về định dạng dữ liệu giống hệt OSRM để không phá vỡ logic tính pin.
    
    Args:
        start_coords: Tuple (lat, lon) điểm bắt đầu
        end_coords: Tuple (lat, lon) điểm kết thúc
        
    Returns:
        list: Danh sách routes theo format OSRM hoặc [] nếu thất bại
    """
    if not MAPBOX_ACCESS_TOKEN or "DÁN_TOKEN" in MAPBOX_ACCESS_TOKEN:
        print("⚠️ Chưa cấu hình Mapbox Token!")
        return []

    try:
        # Mapbox yêu cầu format: Longitude,Latitude (Kinh độ, Vĩ độ)
        # start_coords của bạn đang là (Lat, Lon) nên phải đảo ngược
        start_str = f"{start_coords[1]},{start_coords[0]}"
        end_str = f"{end_coords[1]},{end_coords[0]}"
        
        url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{start_str};{end_str}"
        
        params = {
            "access_token": MAPBOX_ACCESS_TOKEN,
            "geometries": "polyline",  # Lấy encoded polyline giống OSRM
            "overview": "full",
            "steps": "true",           # Lấy chỉ dẫn đường đi
            "alternatives": "false"    # Chỉ lấy 1 đường tốt nhất
        }
        
        print("🌍 Đang gọi Mapbox API (Backup Plan)...")
        response = requests.get(url, params=params, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            routes = data.get("routes", [])
            
            if routes:
                print("✅ Mapbox đã cứu nguy thành công!")
                # Mapbox trả về cấu trúc gần giống OSRM, ta đóng gói lại cho chuẩn
                standardized_routes = []
                for r in routes:
                    # Xử lý steps từ Mapbox format
                    mapbox_steps = []
                    for leg in r.get("legs", []):
                        for step in leg.get("steps", []):
                            mapbox_steps.append({
                                "geometry": step.get("geometry", ""),
                                "distance": step.get("distance", 0),
                                "duration": step.get("duration", 0),
                                "name": step.get("name", ""),
                                "maneuver": step.get("maneuver", {})
                            })
                    
                    standardized_routes.append({
                        "geometry": r["geometry"], 
                        "distance": r["distance"],  # Mapbox cũng trả về mét
                        "duration": r["duration"],  # Mapbox cũng trả về giây
                        "steps": mapbox_steps
                    })
                return standardized_routes
            else:
                print("⚠️ Mapbox không tìm thấy đường đi")
                
        else:
            print(f"❌ Mapbox Error {response.status_code}: {response.text[:100]}")

    except requests.exceptions.Timeout:
        print("⏱️ Mapbox request timeout")
    except Exception as e:
        print(f"❌ Lỗi kết nối Mapbox: {e}")
    
    return []


# ==============================================================================
#  GHI CHÚ: Đã loại bỏ get_route_single (OSRM) 
#  Goong API là routing engine chính, Mapbox là fallback
# ==============================================================================

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


def find_toll_stations_on_route(route_coords, max_distance_km=0.1):
    """
    Tìm trạm thu phí trên đường - Phiên bản Tối ưu Bounding Box
    
    Chiến lược "Vây bắt khoanh vùng":
    1. Bounding Box: Lọc nhanh trạm trong vùng (không tính geodesic cho trạm ở xa)
    2. High Resolution: Quét kỹ với nhiều điểm mẫu hơn (500 thay vì 200)
    3. Khử trùng lặp: 2km radius để tránh Ghost Cost
    
    - max_distance_km: 0.1 (100m) để bắt dính tốt hơn nhưng vẫn tránh đường song song
    """
    if not toll_stations or not route_coords:
        return []
    
    # 1. TẠO BOUNDING BOX (HÌNH CHỮ NHẬT BAO QUANH LỘ TRÌNH)
    # Giúp lọc nhanh, không cần tính khoảng cách cho các trạm ở Hà Nội khi đi Vũng Tàu
    lats = [p[0] for p in route_coords]
    lons = [p[1] for p in route_coords]
    # Buffer 0.02 độ (khoảng 2km) để không bị sót trạm ở mép
    min_lat, max_lat = min(lats) - 0.02, max(lats) + 0.02
    min_lon, max_lon = min(lons) - 0.02, max(lons) + 0.02
    
    # Lọc ra các ứng viên nằm trong vùng
    candidate_tolls = []
    for toll in toll_stations:
        if toll.get('lat') is None or toll.get('lon') is None:
            continue
        if min_lat <= toll['lat'] <= max_lat and min_lon <= toll['lon'] <= max_lon:
            candidate_tolls.append(toll)
            
    if not candidate_tolls:
        return []

    # 2. QUÉT CHI TIẾT (FULL RESOLUTION)
    # Vì đã lọc bớt trạm, ta có thể dùng nhiều điểm mẫu hơn mà không sợ chậm
    # Lấy mẫu mỗi 5 điểm 1 lần (đủ dày để không lọt trạm)
    step = max(1, len(route_coords) // 500) 
    sampled_coords = route_coords[::step]
    
    tolls_on_route = []
    added_toll_coords = []  # Để khử trùng lặp
    
    for toll in candidate_tolls:
        toll_pos = (toll['lat'], toll['lon'])
        
        # --- Khử trùng lặp địa lý (2km) ---
        is_duplicate = False
        for added_pos in added_toll_coords:
            if geodesic(toll_pos, added_pos).km < 2.0:
                is_duplicate = True
                break
        if is_duplicate:
            continue

        # --- Tính khoảng cách ---
        min_dist = float('inf')
        closest_idx = 0
        
        # Tìm điểm gần nhất trên đường
        for i, route_pt in enumerate(sampled_coords):
            d = geodesic(toll_pos, route_pt).km
            if d < min_dist:
                min_dist = d
                closest_idx = i
        
        # Nếu khoảng cách < 100m (0.1km) -> Chấp nhận
        if min_dist <= max_distance_km:
            # Ước lượng km trên lộ trình
            km_on_route = (closest_idx / len(sampled_coords)) * geodesic(route_coords[0], route_coords[-1]).km * 1.3
            
            tolls_on_route.append({
                'station': toll,
                'distance_to_route': round(min_dist * 1000, 0),
                'km_on_route': round(km_on_route, 1),
                'fee': toll.get('fee', DEFAULT_TOLL_FEE)
            })
            added_toll_coords.append(toll_pos)
            
    # Sắp xếp theo vị trí
    tolls_on_route.sort(key=lambda x: x.get('km_on_route', 0))
    return tolls_on_route


def calculate_charging_cost(kwh_charged, station):
    """Tính chi phí sạc tại 1 trạm"""
    price_per_kwh = station.get('price_per_kwh', DEFAULT_PRICE_PER_KWH)
    return kwh_charged * price_per_kwh


def find_ferries_on_route(route_coords, max_distance_km=0.2):
    """
    Tìm phà trên đường - Phiên bản Tối ưu Bounding Box
    
    - max_distance_km: 0.2 (200m) vì phà có thể lệch toạ độ nhiều hơn trạm thu phí
    - Khử trùng lặp 3km: Bến phà thường có 2 đầu (đi/về) cách nhau 1-2km
    """
    if not ferries or not route_coords:
        return []
    
    # 1. Bounding Box Filter
    lats = [p[0] for p in route_coords]
    lons = [p[1] for p in route_coords]
    min_lat, max_lat = min(lats) - 0.02, max(lats) + 0.02
    min_lon, max_lon = min(lons) - 0.02, max(lons) + 0.02
    
    candidate_ferries = [f for f in ferries if 
                         f.get('lat') and f.get('lon') and 
                         min_lat <= f['lat'] <= max_lat and min_lon <= f['lon'] <= max_lon]
    
    if not candidate_ferries:
        return []

    # 2. Detailed Check (High Resolution)
    step = max(1, len(route_coords) // 500)
    sampled_coords = route_coords[::step]
    
    ferries_on_route = []
    added_ferry_coords = []
    
    for ferry in candidate_ferries:
        ferry_pos = (ferry['lat'], ferry['lon'])
        
        # Khử trùng lặp 3km
        is_dup = False
        for added in added_ferry_coords:
            if geodesic(ferry_pos, added).km < 3.0:
                is_dup = True
                break
        if is_dup:
            continue
        
        # Tìm khoảng cách nhỏ nhất
        min_dist = float('inf')
        closest_idx = 0
        for i, pt in enumerate(sampled_coords):
            d = geodesic(ferry_pos, pt).km
            if d < min_dist:
                min_dist = d
                closest_idx = i
        
        # Phà có thể lệch toạ độ nhiều hơn trạm thu phí, để 200m (0.2)
        if min_dist <= max_distance_km:
            # Lấy phí
            fee = ferry.get('fee', 20000)
            if isinstance(fee, str):
                import re
                nums = re.findall(r'[\d,.]+', fee.replace(',', ''))
                fee = int(float(nums[0])) if nums else 20000
            
            # Ước lượng km trên lộ trình
            km_on_route = (closest_idx / len(sampled_coords)) * geodesic(route_coords[0], route_coords[-1]).km * 1.3

            ferries_on_route.append({
                'ferry': ferry,
                'distance_to_route': round(min_dist * 1000, 0),
                'km_on_route': round(km_on_route, 1),
                'fee': fee
            })
            added_ferry_coords.append(ferry_pos)
            
    ferries_on_route.sort(key=lambda x: x.get('km_on_route', 0))
    return ferries_on_route


def find_highway_stations_on_route(route_coords, max_distance_km=0.1):
    """
    Tìm các trạm dừng cao tốc NẰM TRÊN ĐƯỜNG ĐI (trong bán kính 100m)
    
    Trạm dừng cao tốc không tính phí, chỉ để tracking.
    """
    if len(highway_stations) == 0:
        return []
    
    num_samples = min(100, len(route_coords))
    step = max(1, len(route_coords) // num_samples)
    sampled_coords = route_coords[::step]
    
    highways_on_route = []
    seen_highways = set()
    
    for station in highway_stations:
        if station.get('lat') is None or station.get('lon') is None:
            continue
            
        station_pos = (station['lat'], station['lon'])
        station_id = f"{station['lat']:.5f},{station['lon']:.5f}"
        
        if station_id in seen_highways:
            continue
        
        min_distance = float('inf')
        closest_km = 0
        
        for i, coord in enumerate(sampled_coords):
            dist = geodesic(station_pos, coord).km
            if dist < min_distance:
                min_distance = dist
                closest_km = (i / len(sampled_coords)) * geodesic(sampled_coords[0], sampled_coords[-1]).km * 1.3
        
        if min_distance <= max_distance_km:
            seen_highways.add(station_id)
            highways_on_route.append({
                'station': station,
                'distance_to_route': round(min_distance * 1000, 0),
                'km_on_route': round(closest_km, 1)
            })
    
    highways_on_route.sort(key=lambda x: x.get('km_on_route', 0))
    return highways_on_route


def calculate_real_path_length(coords_segment):
    """Tính tổng chiều dài thực tế (km) của một đoạn đường bao gồm các khúc cua"""
    total_km = 0.0
    for i in range(len(coords_segment) - 1):
        total_km += geodesic(coords_segment[i], coords_segment[i+1]).km
    return total_km


# ==============================================================================
#  A* GRAPH-BASED EV ROUTING (Hybrid A* on Route Corridor)
# ==============================================================================
# Thuật toán A* thực sự trên đồ thị các trạm sạc dọc theo lộ trình.
# Nodes: Start, Stations dọc đường, End
# Edges: Đoạn đường giữa các trạm
# Cost (g): Driving Time + Charging Time
# Heuristic (h): Estimated driving time to End
# ==============================================================================

def get_stations_along_route(route_coords, route_km_cumulative, buffer_km=12.0):
    """
    Tìm tất cả trạm sạc trong bán kính buffer_km dọc theo lộ trình.
    
    Chiến lược "Vây lưới" (Corridor Search):
    1. Lấy mẫu các điểm trên route (mỗi ~2km)
    2. Query KD-Tree tìm trạm gần mỗi điểm mẫu
    3. Project mỗi trạm lên route để tính km_position
    
    UPGRADED:
    - buffer_km tăng từ 5km lên 12km để bắt trạm sạc ở đường tránh/nội thị
    - Thêm fallback: nếu không tìm thấy trạm, sẽ tìm 5 trạm gần nhất bất kể khoảng cách
    
    Args:
        route_coords: Danh sách tọa độ [(lat, lon), ...]
        route_km_cumulative: Danh sách km tích lũy tại mỗi điểm [0, 0.5, 1.2, ...]
        buffer_km: Bán kính tìm kiếm (km) - default 12km
        
    Returns:
        list: Danh sách trạm đã được sắp xếp theo km_position
    """
    if station_tree is None or len(stations) == 0:
        print("⚠️ Không có dữ liệu trạm sạc!")
        return []
    
    # 1. LẤY MẪU ROUTE (mỗi ~2km để query nhanh)
    total_route_km = route_km_cumulative[-1] if route_km_cumulative else 0
    sample_interval_km = 2.0  # Lấy mẫu mỗi 2km
    
    sampled_indices = [0]
    last_km = 0
    for i, km in enumerate(route_km_cumulative):
        if km - last_km >= sample_interval_km:
            sampled_indices.append(i)
            last_km = km
    if sampled_indices[-1] != len(route_coords) - 1:
        sampled_indices.append(len(route_coords) - 1)
    
    # 2. QUERY KD-TREE TẠI MỖI ĐIỂM MẪU
    candidate_station_indices = set()
    
    for idx in sampled_indices:
        coord = route_coords[idx]
        try:
            # Tìm 20 trạm gần nhất tại mỗi điểm mẫu (tăng từ 15)
            distances, indices = station_tree.query(coord, k=min(20, len(stations)))
            
            for dist_deg, station_idx in zip(distances, indices):
                station = stations[station_idx]
                # Kiểm tra khoảng cách thực tế (geodesic)
                actual_dist = geodesic(coord, (station['lat'], station['lon'])).km
                if actual_dist <= buffer_km:
                    candidate_station_indices.add(station_idx)
        except Exception as e:
            continue
    
    # === FALLBACK: Nếu không tìm thấy trạm trong hành lang ===
    if not candidate_station_indices:
        print(f"⚠️ Không tìm thấy trạm trong bán kính {buffer_km}km - Đang tìm fallback...")
        
        # Tìm 5 trạm gần nhất tại các checkpoint chính (đầu, giữa, cuối)
        checkpoint_indices = [0, len(sampled_indices)//4, len(sampled_indices)//2, 
                             3*len(sampled_indices)//4, len(sampled_indices)-1]
        
        for cp_idx in checkpoint_indices:
            if cp_idx < len(sampled_indices):
                coord = route_coords[sampled_indices[cp_idx]]
                try:
                    distances, indices = station_tree.query(coord, k=min(3, len(stations)))
                    for station_idx in indices:
                        candidate_station_indices.add(station_idx)
                except:
                    continue
        
        if candidate_station_indices:
            print(f"   ✅ Fallback: Tìm thấy {len(candidate_station_indices)} trạm sạc gần đường đi")
        else:
            print(f"⚠️ Không tìm thấy trạm sạc nào kể cả với fallback!")
            return []
    
    # 3. PROJECT MỖI TRẠM LÊN ROUTE (Linearization)
    candidates = []
    
    for station_idx in candidate_station_indices:
        station = stations[station_idx]
        station_pos = (station['lat'], station['lon'])
        
        # Tìm điểm gần nhất trên route
        min_dist = float('inf')
        best_route_idx = 0
        
        # Chỉ quét các điểm mẫu để tăng tốc (đủ chính xác cho linearization)
        for i in sampled_indices:
            dist = geodesic(station_pos, route_coords[i]).km
            if dist < min_dist:
                min_dist = dist
                best_route_idx = i
        
        # Tính km_position trên route
        km_position = route_km_cumulative[best_route_idx]
        
        # Thêm khoảng cách đi vào trạm (detour distance)
        detour_km = min_dist  # Giả sử đường vào trạm = đường chim bay (an toàn)
        
        candidates.append({
            'station': station,
            'station_idx': station_idx,
            'km_position': km_position,
            'detour_km': detour_km,
            'route_idx': best_route_idx,
            'power': station.get('power', 150),
            'type': 'station'
        })
    
    # 4. SẮP XẾP THEO KM_POSITION
    candidates.sort(key=lambda x: x['km_position'])
    
    print(f"🔍 Tìm thấy {len(candidates)} trạm sạc trong hành lang {buffer_km}km")
    
    return candidates


def build_cumulative_km(route_coords):
    """
    Tính danh sách km tích lũy cho mỗi điểm trên route.
    
    Returns:
        list: [0, 0.5, 1.2, 2.0, ...] (km từ điểm đầu)
    """
    cumulative = [0.0]
    for i in range(1, len(route_coords)):
        dist = geodesic(route_coords[i-1], route_coords[i]).km
        cumulative.append(cumulative[-1] + dist)
    return cumulative


class AStarNode:
    """Node trong A* Search"""
    def __init__(self, node_id, km_position, battery, g_cost, h_cost, parent=None, 
                 charged_at=None, charge_time=0, charge_amount=0):
        self.node_id = node_id  # 'start', 'end', hoặc index trong candidates
        self.km_position = km_position
        self.battery = battery
        self.g_cost = g_cost  # Thời gian thực tế đã đi
        self.h_cost = h_cost  # Heuristic: ước lượng thời gian còn lại
        self.f_cost = g_cost + h_cost
        self.parent = parent
        self.charged_at = charged_at  # Station nếu đã sạc tại đây
        self.charge_time = charge_time
        self.charge_amount = charge_amount  # kWh đã sạc
        
    def __lt__(self, other):
        # Priority queue sắp xếp theo f_cost
        if self.f_cost == other.f_cost:
            return self.h_cost < other.h_cost  # Tie-breaker: ưu tiên gần đích hơn
        return self.f_cost < other.f_cost


def a_star_ev_routing(start_coords, end_coords, route_coords, start_battery, config, priority='time'):
    """
    Thuật toán A* Graph-based cho EV Routing.
    
    Thay vì chia checkpoint cố định 150km, ta:
    1. Tìm tất cả trạm sạc trong "hành lang" 5km dọc route
    2. Xây dựng đồ thị: Start → Stations → End
    3. Chạy A* tìm đường đi tối ưu (minimize total_time)
    
    Args:
        start_coords: Tọa độ xuất phát (lat, lon)
        end_coords: Tọa độ đích (lat, lon)
        route_coords: Polyline đường đi từ Goong API
        start_battery: Pin ban đầu (%)
        config: Cấu hình xe điện
        priority: 'time' hoặc 'cost'
        
    Returns:
        dict: Kết quả routing với charging_stops, metrics, etc.
        error: None hoặc dict mô tả lỗi
    """
    import heapq
    
    # === 1. TIỀN XỬ LÝ: Tính km tích lũy cho route ===
    print(f"\n🔬 === A* GRAPH-BASED EV ROUTING ===")
    route_km_cumulative = build_cumulative_km(route_coords)
    total_route_km = route_km_cumulative[-1]
    
    print(f"📏 Tổng quãng đường: {total_route_km:.1f}km")
    print(f"🔋 Pin xuất phát: {start_battery}%")
    
    # Tính tầm xa hiện tại
    max_range_km = calculate_km_from_battery(start_battery, config)
    print(f"🚗 Tầm xa với pin hiện tại: {max_range_km:.0f}km")
    
    # === 2. TÌM TẤT CẢ TRẠM SẠC DỌC ĐƯỜNG (Corridor Search) ===
    # Buffer 12km để bắt trạm ở đường tránh, nội thị (tăng từ 5km)
    candidates = get_stations_along_route(route_coords, route_km_cumulative, buffer_km=12.0)
    
    # Thêm node Start và End
    nodes = []
    
    # Node Start (index = 0)
    nodes.append({
        'type': 'start',
        'km_position': 0,
        'coords': route_coords[0],
        'detour_km': 0
    })
    
    # Thêm các trạm sạc
    for i, cand in enumerate(candidates):
        nodes.append({
            'type': 'station',
            'km_position': cand['km_position'],
            'coords': (cand['station']['lat'], cand['station']['lon']),
            'station': cand['station'],
            'detour_km': cand['detour_km'],
            'power': cand['power']
        })
    
    # Node End (index = len(nodes))
    nodes.append({
        'type': 'end',
        'km_position': total_route_km,
        'coords': route_coords[-1],
        'detour_km': 0
    })
    
    end_node_idx = len(nodes) - 1
    
    print(f"📊 Đồ thị A*: {len(nodes)} nodes (1 Start + {len(candidates)} Stations + 1 End)")
    
    # === 3. KIỂM TRA TRƯỜNG HỢP ĐẶC BIỆT ===
    # Trường hợp 1: Có thể đi thẳng không cần sạc
    if max_range_km >= total_route_km * 1.05:  # +5% buffer an toàn
        print(f"✅ Có thể đi thẳng đến đích mà không cần sạc!")
        return build_direct_route_result(route_coords, route_km_cumulative, start_battery, config), None
    
    # Trường hợp 2: Không có trạm sạc nào
    if len(candidates) == 0:
        # Tính điểm hết pin dự kiến
        farthest_reachable_km = max_range_km * 0.85
        failure_idx = 0
        for i, km in enumerate(route_km_cumulative):
            if km <= farthest_reachable_km:
                failure_idx = i
            else:
                break
        failure_point = route_coords[failure_idx]
        failure_km = route_km_cumulative[failure_idx]
        
        # Encode polylines
        traveled_coords = route_coords[:failure_idx+1]
        remaining_coords = route_coords[failure_idx:]
        traveled_geometry = polyline.encode(traveled_coords, 5)
        remaining_geometry = polyline.encode(remaining_coords, 5)
        
        # Tìm trạm gần nhất kể cả không với tới
        nearest_station_to_failure = None
        if station_tree is not None and len(stations) > 0:
            try:
                distances, indices = station_tree.query(failure_point, k=1)
                station = stations[indices[0] if isinstance(indices, np.ndarray) else indices]
                dist_to_station = geodesic(failure_point, (station['lat'], station['lon'])).km
                nearest_station_to_failure = {
                    'name': station.get('name', 'Trạm sạc'),
                    'lat': station['lat'],
                    'lon': station['lon'],
                    'power': station.get('power', 150),
                    'distance_km': round(dist_to_station, 1)
                }
            except:
                pass
        
        error_info = {
            "error": "infeasible",
            "error_type": "no_stations_on_route",
            "severity": "critical",
            "message": f"🔋 Không có trạm sạc nào trên tuyến đường!",
            "details": f"Quãng đường {total_route_km:.0f}km nhưng không tìm thấy trạm sạc nào trong bán kính 12km dọc lộ trình.",
            "location": {
                "coords": list(failure_point),
                "km_from_start": round(failure_km, 1),
                "km_to_destination": round(total_route_km - failure_km, 1)
            },
            "progress": {
                "completed_km": round(failure_km, 1),
                "total_km": round(total_route_km, 1),
                "completion_percent": round((failure_km / total_route_km) * 100, 1) if total_route_km > 0 else 0
            },
            "battery_state": {
                "current_percent": start_battery,
                "max_range_km": max_range_km
            },
            "traveled_geometry": traveled_geometry,
            "remaining_geometry": remaining_geometry,
            "failure_point": {
                "lat": failure_point[0],
                "lon": failure_point[1]
            },
            "nearest_station_to_failure": nearest_station_to_failure,
            "suggestions": [
                "Thử tuyến đường khác có nhiều trạm sạc hơn",
                "Sạc đầy pin 100% trước khi xuất phát",
                "Kiểm tra lại dữ liệu trạm sạc trong hệ thống"
            ]
        }
        return None, error_info
    
    # === 4. CHẠY THUẬT TOÁN A* ===
    # Tốc độ trung bình (km/h) để tính heuristic
    AVG_SPEED_KMH = 60.0
    SAFETY_FACTOR = 1.05  # Hệ số an toàn cho tiêu hao pin
    
    # Priority Queue: (f_cost, counter, node)
    # counter để tránh so sánh node khi f_cost bằng nhau
    pq = []
    counter = 0
    
    # Node khởi đầu
    h_start = (total_route_km / AVG_SPEED_KMH) * 60  # phút
    start_node = AStarNode(
        node_id=0,
        km_position=0,
        battery=start_battery,
        g_cost=0,
        h_cost=h_start,
        parent=None
    )
    heapq.heappush(pq, (start_node.f_cost, counter, start_node))
    counter += 1
    
    # Visited states: {(node_idx, battery_bucket): min_g_cost}
    # battery_bucket = int(battery / 5) để gom nhóm trạng thái tương tự
    visited = {}
    
    best_solution = None
    iterations = 0
    max_iterations = 150000  # Tăng từ 50k lên 150k cho tuyến dài (HCM-Nha Trang)
    
    while pq and iterations < max_iterations:
        iterations += 1
        _, _, current = heapq.heappop(pq)
        
        # === GOAL CHECK ===
        if current.node_id == end_node_idx:
            if best_solution is None or current.g_cost < best_solution.g_cost:
                best_solution = current
                print(f"🎯 Tìm thấy lộ trình! Total time: {current.g_cost:.0f} phút")
            continue  # Tiếp tục tìm giải pháp tốt hơn
        
        # === PRUNING: Kiểm tra visited ===
        battery_bucket = int(current.battery / 5)
        state_key = (current.node_id, battery_bucket)
        
        if state_key in visited and visited[state_key] <= current.g_cost:
            continue
        visited[state_key] = current.g_cost
        
        current_node = nodes[current.node_id]
        
        # === EXPAND: Thử đi đến các node phía trước ===
        for next_idx in range(current.node_id + 1, len(nodes)):
            next_node = nodes[next_idx]
            
            # Tính khoảng cách đến next_node
            dist_on_route = next_node['km_position'] - current_node['km_position']
            detour_dist = next_node.get('detour_km', 0) * 2  # Đi vào + ra
            total_dist = (dist_on_route + detour_dist) * SAFETY_FACTOR
            
            # Tính pin tiêu thụ
            battery_consumed = calculate_battery_consumption(total_dist, config)
            battery_remaining = current.battery - battery_consumed
            
            # Nếu không đủ pin đến next_node
            if battery_remaining < config['min_battery']:
                # Nếu current_node là station, thử sạc trước khi đi
                if current_node['type'] == 'station':
                    # Tính lượng sạc cần thiết
                    battery_needed = battery_consumed + config['min_battery'] + 10  # +10% buffer
                    charge_needed = max(0, battery_needed - current.battery)
                    
                    if charge_needed > 0 and current.battery < 100:
                        # Tính thời gian sạc
                        target_battery = min(100, current.battery + charge_needed)
                        charge_time = calculate_charge_time(
                            current.battery, 
                            target_battery, 
                            current_node.get('power', 150), 
                            config
                        )
                        
                        # Tính lại battery sau khi sạc
                        new_battery = target_battery - battery_consumed
                        
                        if new_battery >= config['min_battery']:
                            # Tạo node mới với việc sạc
                            driving_time = (total_dist / AVG_SPEED_KMH) * 60
                            new_g_cost = current.g_cost + driving_time + charge_time
                            
                            # Heuristic
                            remaining_km = total_route_km - next_node['km_position']
                            h_cost = (remaining_km / AVG_SPEED_KMH) * 60
                            
                            energy_kwh = (charge_needed / 100) * config['battery_capacity']
                            
                            new_node = AStarNode(
                                node_id=next_idx,
                                km_position=next_node['km_position'],
                                battery=new_battery,
                                g_cost=new_g_cost,
                                h_cost=h_cost,
                                parent=current,
                                charged_at=current_node.get('station'),
                                charge_time=charge_time,
                                charge_amount=energy_kwh
                            )
                            
                            heapq.heappush(pq, (new_node.f_cost, counter, new_node))
                            counter += 1
                
                # Không thể đến next_node từ current_node mà không sạc
                # (hoặc current_node không phải station)
                continue
            
            # === CÓ THỂ ĐI ĐƯỢC ===
            driving_time = (total_dist / AVG_SPEED_KMH) * 60
            new_g_cost = current.g_cost + driving_time
            
            # Heuristic
            remaining_km = total_route_km - next_node['km_position']
            h_cost = (remaining_km / AVG_SPEED_KMH) * 60
            
            new_node = AStarNode(
                node_id=next_idx,
                km_position=next_node['km_position'],
                battery=battery_remaining,
                g_cost=new_g_cost,
                h_cost=h_cost,
                parent=current
            )
            
            heapq.heappush(pq, (new_node.f_cost, counter, new_node))
            counter += 1
    
    print(f"🔄 A* iterations: {iterations}")
    
    # === 5. XÂY DỰNG KẾT QUẢ TỪ BEST SOLUTION ===
    if best_solution is None:
        # Không tìm thấy đường đi - TRẢ VỀ ENHANCED INFEASIBILITY RESPONSE
        
        # Tìm điểm dừng cuối cùng có thể đi được (điểm hết pin)
        farthest_reachable_km = max_range_km * 0.85  # 85% tầm xa thực tế
        failure_idx = 0
        
        for i, km in enumerate(route_km_cumulative):
            if km <= farthest_reachable_km:
                failure_idx = i
            else:
                break
        
        failure_point = route_coords[failure_idx]
        failure_km = route_km_cumulative[failure_idx]
        
        # Chia polyline thành traveled (đi được) và remaining (còn lại)
        traveled_coords = route_coords[:failure_idx+1]
        remaining_coords = route_coords[failure_idx:]
        
        # Encode polylines sử dụng thư viện polyline
        traveled_geometry = polyline.encode(traveled_coords, 5)
        remaining_geometry = polyline.encode(remaining_coords, 5)
        
        # Tìm trạm sạc gần nhất với điểm hết pin (kể cả không với tới được)
        nearest_station_to_failure = None
        if station_tree is not None and len(stations) > 0:
            try:
                distances, indices = station_tree.query(failure_point, k=3)
                for station_idx in indices:
                    station = stations[station_idx]
                    dist_to_station = geodesic(failure_point, (station['lat'], station['lon'])).km
                    nearest_station_to_failure = {
                        'name': station.get('name', 'Trạm sạc'),
                        'lat': station['lat'],
                        'lon': station['lon'],
                        'power': station.get('power', 150),
                        'distance_km': round(dist_to_station, 1),
                        'unreachable_by_km': round(dist_to_station - (max_range_km - failure_km), 1)
                    }
                    break
            except:
                pass
        
        error_info = {
            "error": "infeasible",
            "error_type": "no_feasible_path",
            "severity": "critical",
            "message": f"🔋 Không tìm được lộ trình khả thi!",
            "details": f"Thuật toán A* không tìm được đường đi với pin {start_battery}% trên quãng đường {total_route_km:.0f}km.",
            "battery_state": {
                "current_percent": start_battery,
                "max_range_km": max_range_km
            },
            "location": {
                "coords": list(failure_point),
                "km_from_start": round(failure_km, 1),
                "km_to_destination": round(total_route_km - failure_km, 1)
            },
            "progress": {
                "completed_km": round(failure_km, 1),
                "total_km": round(total_route_km, 1),
                "completion_percent": round((failure_km / total_route_km) * 100, 1) if total_route_km > 0 else 0
            },
            # ENHANCED: Polyline geometry cho map visualization
            "traveled_geometry": traveled_geometry,
            "remaining_geometry": remaining_geometry,
            "failure_point": {
                "lat": failure_point[0],
                "lon": failure_point[1]
            },
            "nearest_station_to_failure": nearest_station_to_failure,
            "suggestions": [
                f"Sạc pin lên ít nhất {min(100, int((total_route_km / max_range_km) * 100) + 20)}% trước khi xuất phát",
                "Chọn xe có dung lượng pin lớn hơn",
                "Thử tuyến đường có nhiều trạm sạc hơn"
            ]
        }
        return None, error_info
    
    # Reconstruct path
    return reconstruct_astar_result(best_solution, nodes, route_coords, route_km_cumulative, config)


def reconstruct_astar_result(goal_node, nodes, route_coords, route_km_cumulative, config):
    """
    Xây dựng kết quả từ A* solution path.
    
    Returns:
        dict: Kết quả với format giống hệt phiên bản cũ
        None: error (nếu có)
    """
    total_route_km = route_km_cumulative[-1]
    
    # Reconstruct path từ goal về start
    path = []
    current = goal_node
    while current is not None:
        path.append(current)
        current = current.parent
    path.reverse()
    
    # Thu thập charging stops
    charging_stops = []
    total_charging_cost = 0
    travel_events = []
    
    for node in path:
        if node.charged_at is not None:
            station = node.charged_at
            
            # Tìm km_position của trạm sạc
            station_km = 0
            for n in nodes:
                if n['type'] == 'station' and n.get('station') == station:
                    station_km = n['km_position']
                    break
            
            # Tính chi phí sạc
            charging_cost = calculate_charging_cost(node.charge_amount, station)
            total_charging_cost += charging_cost
            
            # Tính arrival battery (battery trước khi sạc)
            arrival_battery = goal_node.battery  # Fallback
            for i, n in enumerate(path):
                if n == node and i > 0:
                    # Battery khi đến = battery của node trước - consumed
                    prev_node = path[i-1]
                    prev_km = nodes[prev_node.node_id]['km_position']
                    curr_km = station_km
                    dist = (curr_km - prev_km) * 1.05
                    consumed = calculate_battery_consumption(dist, config)
                    arrival_battery = prev_node.battery - consumed
                    break
            
            charging_stop_info = {
                'station': station,
                'arrival_battery': round(max(0, arrival_battery), 1),
                'start_charge': round(max(0, arrival_battery), 1),
                'end_charge': round(arrival_battery + (node.charge_amount / config['battery_capacity'] * 100), 1),
                'charge_time': round(node.charge_time, 1),
                'distance_from_start': round(station_km, 1),
                'energy_kwh': round(node.charge_amount, 2),
                'charging_cost': round(charging_cost, 0),
                'price_per_kwh': station.get('price_per_kwh', DEFAULT_PRICE_PER_KWH)
            }
            charging_stops.append(charging_stop_info)
            
            travel_events.append({
                'type': 'charging',
                'name': station.get('name', 'Trạm sạc'),
                'cost': round(charging_cost, 0),
                'km': round(station_km, 1),
                'coords': [station['lat'], station['lon']],
                'details': {
                    'energy_kwh': round(node.charge_amount, 2),
                    'charge_time_min': round(node.charge_time, 1),
                    'from_percent': round(max(0, arrival_battery), 1),
                    'to_percent': round(arrival_battery + (node.charge_amount / config['battery_capacity'] * 100), 1),
                    'power_kw': station.get('power', 150)
                }
            })
    
    # Tính phí cầu đường, phà, trạm dừng cao tốc
    tolls_on_route = find_toll_stations_on_route(route_coords, max_distance_km=0.1)
    ferries_on_route = find_ferries_on_route(route_coords, max_distance_km=0.2)
    highways_on_route = find_highway_stations_on_route(route_coords, max_distance_km=0.1)
    
    total_toll_cost = 0
    toll_stations_passed = []
    
    for toll_info in tolls_on_route:
        toll = toll_info['station']
        fee = toll_info.get('fee', DEFAULT_TOLL_FEE)
        if isinstance(fee, str):
            import re
            numbers = re.findall(r'[\d,.]+', fee.replace(',', ''))
            fee = int(float(numbers[0])) if numbers else DEFAULT_TOLL_FEE
        
        total_toll_cost += fee
        toll_stations_passed.append({
            'name': toll.get('name', 'Trạm thu phí'),
            'highway': toll.get('highway', 'N/A'),
            'fee': fee,
            'km': toll_info.get('km_on_route', 0)
        })
        
        travel_events.append({
            'type': 'toll',
            'name': toll.get('name', 'Trạm thu phí'),
            'cost': fee,
            'km': toll_info.get('km_on_route', 0),
            'coords': [toll['lat'], toll['lon']],
            'details': {'highway': toll.get('highway', 'N/A')}
        })
    
    total_ferry_cost = 0
    ferries_passed = []
    
    for ferry_info in ferries_on_route:
        ferry = ferry_info['ferry']
        fee = ferry_info.get('fee', 50000)
        total_ferry_cost += fee
        ferries_passed.append({
            'name': ferry.get('name', 'Phà'),
            'fee': fee,
            'km': ferry_info.get('km_on_route', 0)
        })
        
        travel_events.append({
            'type': 'ferry',
            'name': ferry.get('name', 'Phà'),
            'cost': fee,
            'km': ferry_info.get('km_on_route', 0),
            'coords': [ferry['lat'], ferry['lon']],
            'details': {'crossing_time_min': ferry.get('crossing_time', 15)}
        })
    
    highway_stops_passed = []
    for hw_info in highways_on_route:
        station = hw_info['station']
        highway_stops_passed.append({
            'name': station.get('name', 'Trạm dừng'),
            'km': hw_info.get('km_on_route', 0)
        })
        
        travel_events.append({
            'type': 'highway_stop',
            'name': station.get('name', 'Trạm dừng cao tốc'),
            'cost': 0,
            'km': hw_info.get('km_on_route', 0),
            'coords': [station['lat'], station['lon']],
            'details': {'amenities': station.get('amenities', [])}
        })
    
    # Sắp xếp travel events theo km
    travel_events.sort(key=lambda x: x['km'])
    
    # Tính thời gian
    driving_time = goal_node.g_cost - sum(s['charge_time'] for s in charging_stops)
    charging_time = sum(s['charge_time'] for s in charging_stops)
    total_cost = total_charging_cost + total_toll_cost + total_ferry_cost
    
    print(f"   💰 Phát hiện {len(toll_stations_passed)} trạm thu phí trên đường đi")
    print(f"   🚢 Phát hiện {len(ferries_passed)} phà trên đường đi")
    
    return {
        'total_distance': round(total_route_km, 2),
        'driving_time': round(driving_time, 1),
        'charging_time': round(charging_time, 1),
        'total_time': round(goal_node.g_cost, 1),
        'num_charges': len(charging_stops),
        'charging_stops': charging_stops,
        'final_battery': round(max(0, goal_node.battery), 1),
        'feasible': True,
        
        # Chi phí
        'charging_cost': round(total_charging_cost, 0),
        'toll_cost': round(total_toll_cost, 0),
        'ferry_cost': round(total_ferry_cost, 0),
        'total_cost': round(total_cost, 0),
        
        # Chi tiết các trạm
        'toll_stations_passed': toll_stations_passed,
        'num_tolls': len(toll_stations_passed),
        'ferries_passed': ferries_passed,
        'num_ferries': len(ferries_passed),
        'highway_stops_passed': highway_stops_passed,
        'num_highway_stops': len(highway_stops_passed),
        
        # Travel events
        'travel_events': travel_events,
        
        # A* metadata (để debug/bảo vệ đồ án)
        'algorithm': 'A* Graph-based',
        'nodes_explored': len([n for n in [goal_node] if n])
    }, None


def build_direct_route_result(route_coords, route_km_cumulative, start_battery, config):
    """
    Xây dựng kết quả khi có thể đi thẳng không cần sạc.
    """
    total_route_km = route_km_cumulative[-1]
    battery_consumed = calculate_battery_consumption(total_route_km, config)
    final_battery = start_battery - battery_consumed
    
    # Tính phí cầu đường, phà
    tolls_on_route = find_toll_stations_on_route(route_coords, max_distance_km=0.1)
    ferries_on_route = find_ferries_on_route(route_coords, max_distance_km=0.2)
    highways_on_route = find_highway_stations_on_route(route_coords, max_distance_km=0.1)
    
    total_toll_cost = 0
    toll_stations_passed = []
    travel_events = []
    
    for toll_info in tolls_on_route:
        toll = toll_info['station']
        fee = toll_info.get('fee', DEFAULT_TOLL_FEE)
        if isinstance(fee, str):
            import re
            numbers = re.findall(r'[\d,.]+', fee.replace(',', ''))
            fee = int(float(numbers[0])) if numbers else DEFAULT_TOLL_FEE
        total_toll_cost += fee
        toll_stations_passed.append({
            'name': toll.get('name', 'Trạm thu phí'),
            'highway': toll.get('highway', 'N/A'),
            'fee': fee,
            'km': toll_info.get('km_on_route', 0)
        })
        travel_events.append({
            'type': 'toll', 'name': toll.get('name'), 'cost': fee,
            'km': toll_info.get('km_on_route', 0),
            'coords': [toll['lat'], toll['lon']],
            'details': {'highway': toll.get('highway', 'N/A')}
        })
    
    total_ferry_cost = 0
    ferries_passed = []
    for ferry_info in ferries_on_route:
        ferry = ferry_info['ferry']
        fee = ferry_info.get('fee', 50000)
        total_ferry_cost += fee
        ferries_passed.append({
            'name': ferry.get('name', 'Phà'),
            'fee': fee,
            'km': ferry_info.get('km_on_route', 0)
        })
        travel_events.append({
            'type': 'ferry', 'name': ferry.get('name'), 'cost': fee,
            'km': ferry_info.get('km_on_route', 0),
            'coords': [ferry['lat'], ferry['lon']],
            'details': {}
        })
    
    highway_stops_passed = []
    for hw_info in highways_on_route:
        station = hw_info['station']
        highway_stops_passed.append({
            'name': station.get('name', 'Trạm dừng'),
            'km': hw_info.get('km_on_route', 0)
        })
        travel_events.append({
            'type': 'highway_stop', 'name': station.get('name'), 'cost': 0,
            'km': hw_info.get('km_on_route', 0),
            'coords': [station['lat'], station['lon']],
            'details': {}
        })
    
    travel_events.sort(key=lambda x: x['km'])
    
    driving_time = (total_route_km / 60) * 60
    
    print(f"   💰 Phát hiện {len(toll_stations_passed)} trạm thu phí trên đường đi")
    print(f"   🚢 Phát hiện {len(ferries_passed)} phà trên đường đi")
    
    return {
        'total_distance': round(total_route_km, 2),
        'driving_time': round(driving_time, 1),
        'charging_time': 0,
        'total_time': round(driving_time, 1),
        'num_charges': 0,
        'charging_stops': [],
        'final_battery': round(max(0, final_battery), 1),
        'feasible': True,
        'charging_cost': 0,
        'toll_cost': round(total_toll_cost, 0),
        'ferry_cost': round(total_ferry_cost, 0),
        'total_cost': round(total_toll_cost + total_ferry_cost, 0),
        'toll_stations_passed': toll_stations_passed,
        'num_tolls': len(toll_stations_passed),
        'ferries_passed': ferries_passed,
        'num_ferries': len(ferries_passed),
        'highway_stops_passed': highway_stops_passed,
        'num_highway_stops': len(highway_stops_passed),
        'travel_events': travel_events,
        'algorithm': 'Direct (No charging needed)'
    }

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

def add_route_to_map(map_obj, route_coords, charging_stops, travel_events=None, color="blue", label=""):
    """
    Thêm route và CÁC SỰ KIỆN (Thu phí, Phà, Trạm dừng) lên bản đồ.
    Đã sửa: Vẽ Full tọa độ để bám sát đường đi thực tế.
    
    Args:
        map_obj: Folium map object
        route_coords: Danh sách tọa độ đường đi
        charging_stops: Danh sách các trạm sạc
        travel_events: Danh sách sự kiện (toll, ferry, highway_stop)
        color: Màu đường đi
        label: Nhãn route
    """
    # 1. VẼ ĐƯỜNG ĐI (Polyline) - KHÔNG LẤY MẪU (NO SAMPLING)
    # Thay vì lấy mẫu 100 điểm, ta vẽ toàn bộ điểm để đường cong mềm mại
    
    folium.PolyLine(
        route_coords,  # <--- Dùng trực tiếp toàn bộ danh sách tọa độ gốc
        color=color, 
        weight=5,
        opacity=0.7,
        popup=label, 
        smooth_factor=1.0  # <--- Giảm xuống 1.0 để Folium không tự động làm thẳng các khúc cua
    ).add_to(map_obj)
    
    # 2. ĐIỂM XUẤT PHÁT & ĐÍCH
    folium.Marker(
        route_coords[0], 
        icon=folium.Icon(color="green", icon="play", prefix='fa'),
        popup=folium.Popup("<b>🚗 Xuất phát</b>", max_width=150)
    ).add_to(map_obj)
    
    folium.Marker(
        route_coords[-1], 
        icon=folium.Icon(color="red", icon="flag-checkered", prefix='fa'),
        popup=folium.Popup("<b>🏁 Đích</b>", max_width=150)
    ).add_to(map_obj)
    
    # 3. VẼ TRẠM SẠC ĐÃ CHỌN
    for i, stop in enumerate(charging_stops, 1):
        s = stop['station']
        
        popup_content = f"""
            <div style='min-width: 220px; font-family: Arial;'>
                <b style='color: #f97316; font-size: 14px;'>⚡ TRẠM SẠC #{i}</b><br>
                <b>{s['name']}</b><br>
                <small>🔌 {s.get('power', 150)}kW | ⏱️ {stop['charge_time']:.0f}p</small><br>
                <small>🔋 {stop['start_charge']:.0f}% ➝ {stop['end_charge']:.0f}%</small>
            </div>
        """
        
        folium.Marker(
            [s['lat'], s['lon']],
            icon=folium.Icon(color="orange", icon="bolt", prefix='fa'),
            popup=folium.Popup(popup_content, max_width=250),
            tooltip=f"⚡ Trạm sạc #{i}",
            zIndexOffset=1000  # Ưu tiên hiển thị lên trên
        ).add_to(map_obj)
    
    # 4. VẼ TRAVEL EVENTS (Trạm thu phí, Phà, Trạm dừng cao tốc)
    if travel_events:
        for event in travel_events:
            # Bỏ qua nếu không có tọa độ
            if 'coords' not in event or not event['coords']:
                continue
            
            coords = event['coords']
            event_type = event.get('type', 'unknown')
            
            if event_type == 'toll':
                # === TRẠM THU PHÍ ===
                popup_content = f"""
                    <div style='min-width: 180px; font-family: Arial;'>
                        <b style='color: #dc2626; font-size: 13px;'>💰 TRẠM THU PHÍ</b><br>
                        <b>{event['name']}</b><br>
                        <span style='color: #b91c1c; font-weight: 700;'>Phí: {event['cost']:,.0f} đ</span><br>
                        <small style='color: gray;'>📍 Km {event['km']:.1f}</small>
                    </div>
                """
                folium.Marker(
                    coords,
                    icon=folium.Icon(color="red", icon="money-bill", prefix='fa', icon_size=(12, 12)), # Chỉnh icon to hơn xíu để dễ nhìn
                    popup=folium.Popup(popup_content, max_width=200),
                    tooltip=f"💰 {event['name']}"
                ).add_to(map_obj)
                
            elif event_type == 'ferry':
                # === PHÀ ===
                popup_content = f"""
                    <div style='min-width: 180px; font-family: Arial;'>
                        <b style='color: #2563eb; font-size: 13px;'>🚢 PHÀ</b><br>
                        <b>{event['name']}</b><br>
                        <span style='color: #1d4ed8; font-weight: 700;'>Vé: {event['cost']:,.0f} đ</span><br>
                        <small style='color: gray;'>📍 Km {event['km']:.1f}</small>
                    </div>
                """
                folium.Marker(
                    coords,
                    icon=folium.Icon(color="blue", icon="ship", prefix='fa', icon_size=(12, 12)),
                    popup=folium.Popup(popup_content, max_width=200),
                    tooltip=f"🚢 {event['name']}"
                ).add_to(map_obj)
                
            elif event_type == 'highway_stop':
                # === TRẠM DỪNG CAO TỐC ===
                popup_content = f"""
                    <div style='min-width: 180px; font-family: Arial;'>
                        <b style='color: #d97706; font-size: 13px;'>🛣️ TRẠM DỪNG</b><br>
                        <b>{event['name']}</b><br>
                        <small style='color: gray;'>📍 Km {event['km']:.1f}</small>
                    </div>
                """
                folium.Marker(
                    coords,
                    icon=folium.Icon(color="beige", icon="gas-pump", prefix='fa'),
                    popup=folium.Popup(popup_content, max_width=200),
                    tooltip=f"🛣️ {event['name']}"
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
            return jsonify({
                "error": "Không tìm thấy địa điểm xuất phát",
                "error_type": "geocode_failed",
                "details": f"Không thể định vị địa chỉ: '{start_addr}'. Goong/Nominatim không nhận diện được địa điểm này.",
                "field": "start",
                "suggestions": [
                    "Nhập địa chỉ cụ thể hơn (số nhà, tên đường, quận/huyện)",
                    "Sử dụng tên địa điểm phổ biến (Bến xe, Sân bay, Bệnh viện...)",
                    "Định dạng chuẩn: 'Số nhà Tên đường, Quận/Huyện, Tỉnh/TP'"
                ]
            }), 400
        
        print(f"🔄 Đang geocode '{end_addr}'...")
        end_loc = geolocator.geocode(end_addr + ", Việt Nam", timeout=15)
        
        if not end_loc:
            print(f"❌ Không tìm thấy địa chỉ đến: {end_addr}")
            return jsonify({
                "error": "Không tìm thấy địa điểm đến",
                "error_type": "geocode_failed", 
                "details": f"Không thể định vị địa chỉ: '{end_addr}'. Goong/Nominatim không nhận diện được địa điểm này.",
                "field": "end",
                "suggestions": [
                    "Nhập địa chỉ cụ thể hơn (số nhà, tên đường, quận/huyện)",
                    "Sử dụng tên địa điểm phổ biến (Bến xe, Sân bay, Bệnh viện...)",
                    "Định dạng chuẩn: 'Số nhà Tên đường, Quận/Huyện, Tỉnh/TP'"
                ]
            }), 400
        
        print(f"⏱️ Geocoding: {time.time() - geocode_start:.1f}s")
        
        start_coords = (start_loc.latitude, start_loc.longitude)
        end_coords = (end_loc.latitude, end_loc.longitude)
        
        print(f"✅ Start: {start_coords[0]:.4f}, {start_coords[1]:.4f}")
        print(f"✅ End: {end_coords[0]:.4f}, {end_coords[1]:.4f}")
        
        direct_distance = geodesic(start_coords, end_coords).km
        print(f"📏 Khoảng cách đường chim bay: {direct_distance:.0f}km")
        
        if direct_distance < 1:
            return jsonify({
                "error": "Hai điểm quá gần nhau",
                "error_type": "distance_too_short",
                "details": f"Khoảng cách giữa điểm đi và điểm đến chỉ {direct_distance*1000:.0f}m (< 1km). Hệ thống tối ưu cho lộ trình > 10km.",
                "suggestions": [
                    "Nhập hai địa điểm cách xa nhau hơn",
                    "Hệ thống này dành cho lộ trình liên tỉnh/thành phố"
                ]
            }), 400
        
        if direct_distance > 5000:
            print(f"LỖI: Khoảng cách quá xa ({direct_distance:.0f}km > 5000km)")
            return jsonify({
                "error": "Khoảng cách quá xa hoặc điểm đến ngoài Việt Nam",
                "error_type": "distance_too_far",
                "details": f"Khoảng cách đường chim bay: {direct_distance:.0f}km. Hệ thống chỉ hỗ trợ lộ trình trong phạm vi Việt Nam (< 5000km).",
                "route_info": {
                    "start": start_addr,
                    "end": end_addr,
                    "start_coords": f"{start_coords[0]:.4f}, {start_coords[1]:.4f}",
                    "end_coords": f"{end_coords[0]:.4f}, {end_coords[1]:.4f}",
                    "direct_distance_km": round(direct_distance, 1)
                },
                "suggestions": [
                    "Kiểm tra lại địa chỉ điểm đến có đúng không",
                    "Đảm bảo cả hai điểm đều nằm trong lãnh thổ Việt Nam",
                    "Có thể bạn đã nhập nhầm quốc gia hoặc châu lục",
                    "Thử nhập lại với tên thành phố/tỉnh cụ thể ở Việt Nam"
                ]
            }), 400
        
        if direct_distance > 1500:
            print(f"CẢNH BÁO: Quãng đường rất xa ({direct_distance:.0f}km)")
        
        # GET ROUTE
        route_start = time.time()
        print(f"Đang lấy route từ Goong API...")
        base_routes, route_error = get_route(start_coords, end_coords, data)
        print(f"Routing API: {time.time() - route_start:.1f}s")
        
        if not base_routes:
            print(f"❌ Không thể lấy route từ Goong/Mapbox")
            error_detail = route_error if route_error else "Không rõ nguyên nhân"
            return jsonify({
                "error": "Không thể tìm đường đi",
                "error_type": "routing_failed",
                "details": f"Goong API và Mapbox đều không thể tính toán lộ trình cho tuyến đường này. Lỗi chi tiết: {error_detail}",
                "route_info": {
                    "start": start_addr,
                    "end": end_addr,
                    "direct_distance_km": round(direct_distance, 1)
                },
                "suggestions": [
                    "Thử lại sau vài giây",
                    "Kiểm tra kết nối internet",
                    f"Quãng đường {direct_distance:.0f}km - có thể cần chia nhỏ chuyến đi",
                    "Thử nhập địa chỉ khác gần khu vực đó"
                ]
            }), 400
        
        print(f"✅ Nhận được {len(base_routes)} route từ Goong API")
        
        # TÍNH TOÁN TRẠM SẠC
        calc_start = time.time()
        routes_data = []
        last_infeasibility_error = None  # Lưu lại infeasibility error cuối cùng
        
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
                    
                    # === LOGIC MỚI: PHÂN TÍCH HIỆU QUẢ BỘ LỌC ===
                    filter_feedback = []
                    
                    # 1. Kiểm tra bộ lọc Phí cầu đường
                    if data.get('avoid_tolls'):
                        if result['num_tolls'] == 0:
                            filter_feedback.append({
                                'type': 'success',
                                'icon': '💸',
                                'text': 'Đã tránh hoàn toàn trạm thu phí'
                            })
                        else:
                            filter_feedback.append({
                                'type': 'warning',
                                'icon': '⚠️',
                                'text': f"Vẫn phải qua {result['num_tolls']} trạm thu phí (bắt buộc)"
                            })
                    
                    # 2. Kiểm tra bộ lọc Phà
                    if data.get('avoid_ferries'):
                        if result['num_ferries'] == 0:
                            filter_feedback.append({
                                'type': 'success',
                                'icon': '⚓',
                                'text': 'Đã tránh đi qua phà'
                            })
                        else:
                            filter_feedback.append({
                                'type': 'warning',
                                'icon': '⚠️',
                                'text': f"Vẫn phải đi qua {result['num_ferries']} bến phà"
                            })

                    # 3. Kiểm tra bộ lọc Cao tốc
                    if data.get('avoid_highways'):
                        filter_feedback.append({
                            'type': 'info',
                            'icon': '🛣️',
                            'text': 'Đã ưu tiên đường dân sinh (tránh cao tốc)'
                        })
                    
                    # KHÔNG gán name/color cố định ở đây - sẽ gán ĐỘNG sau khi sort
                    routes_data.append({
                        'name': f'Route {idx + 1}',  # Tạm thời, sẽ đổi sau sort
                        'route': base_route, 
                        'coords': route_coords,
                        'stops': result['charging_stops'], 
                        'metrics': result, 
                        'feasible': True,
                        'color': '#94a3b8',  # Tạm thời màu xám, sẽ đổi sau sort
                        'is_best': False,    # Sẽ xác định sau sort
                        'instructions': turn_by_turn,
                        'segments': road_segments,
                        'filter_feedback': filter_feedback
                    })
                else:
                    print(f"   ❌ Không khả thi: {err}")
                    # === LƯU LẠI INFEASIBILITY ERROR ĐỂ TRẢ VỀ CHO FRONTEND ===
                    if isinstance(err, dict) and err.get('error') == 'infeasible':
                        last_infeasibility_error = err
            except Exception as e:
                print(f"   ❌ Lỗi xử lý route {idx}: {e}")
                traceback.print_exc()
                continue
        
        print(f"⏱️ Tính trạm sạc: {time.time() - calc_start:.1f}s")
        
        if not routes_data:
            print(f"❌ KHÔNG CÓ LỘ TRÌNH KHẢ THI")
            current_battery = float(data.get('start_battery', 100))
            km_can_travel = calculate_km_from_battery(current_battery, EV_CONFIG)
            
            # === CẢI TIỆN: TRẢ VỀ INFEASIBILITY OBJECT CHO FRONTEND ===
            # Nếu có infeasibility error chi tiết từ a_star_ev_routing, trả về nó
            if last_infeasibility_error:
                print(f"   📊 Trả về infeasibility error chi tiết cho Frontend")
                
                # === TẠO MAP HTML CHO INFEASIBLE ROUTE ===
                try:
                    # Decode polyline để lấy tọa độ
                    traveled_coords = polyline.decode(last_infeasibility_error.get('traveled_geometry', '')) if last_infeasibility_error.get('traveled_geometry') else []
                    remaining_coords = polyline.decode(last_infeasibility_error.get('remaining_geometry', '')) if last_infeasibility_error.get('remaining_geometry') else []
                    
                    # Lấy failure point
                    fail_pt = None
                    if last_infeasibility_error.get('failure_point'):
                        fp = last_infeasibility_error['failure_point']
                        fail_pt = (fp['lat'], fp['lon'])
                    
                    # Tạo map HTML
                    error_map_html = create_infeasible_map(
                        start_coords, 
                        end_coords, 
                        traveled_coords, 
                        remaining_coords, 
                        fail_pt,
                        last_infeasibility_error.get('nearest_station_to_failure')
                    )
                    
                    # Gán map vào response
                    last_infeasibility_error['map_html'] = error_map_html
                    print(f"   ✅ Đã tạo infeasibility map HTML")
                    
                except Exception as e:
                    print(f"   ⚠️ Lỗi khi vẽ map infeasible: {e}")
                    last_infeasibility_error['map_html'] = None
                
                return jsonify({
                    "infeasibility": last_infeasibility_error
                }), 200  # Trả 200 OK vì đây là kết quả tính toán hợp lệ, không phải lỗi server
            
            # Fallback: Tạo infeasibility object mặc định nếu không có chi tiết
            default_infeasibility = {
                "error": "infeasible",
                "error_type": "no_feasible_route",
                "severity": "critical",
                
                "location": {
                    "km_from_start": 0,
                    "km_to_destination": round(direct_distance, 1)
                },
                
                "battery_state": {
                    "current_percent": round(current_battery, 1),
                    "max_range_km": round(km_can_travel, 1),
                    "needed_km": round(direct_distance, 1),
                    "deficit_percent": round(max(0, (direct_distance - km_can_travel) / km_can_travel * 100), 1) if km_can_travel > 0 else 100
                },
                
                "suggestions": [
                    "Sạc pin lên 100% trước khi xuất phát",
                    "Chọn xe có dung lượng pin lớn hơn",
                    "Chia chuyến đi thành nhiều chặng ngắn hơn"
                ],
                
                "message": f"🔋 Không có lộ trình khả thi!",
                "details": f"Quãng đường {direct_distance:.0f}km, pin {current_battery}% (đi được ~{km_can_travel:.0f}km). Không có trạm sạc phù hợp trên đường đi.",
                "map_html": None  # No map for default case
            }
            
            return jsonify({
                "infeasibility": default_infeasibility
            }), 200
        
        # === DYNAMIC SORTING THEO PRIORITY ===
        priority = data.get('priority', 'time')
        print(f"📊 Sắp xếp theo tiêu chí: {priority}")
        
        if priority == 'time':
            # Tổng thời gian ít nhất
            routes_data.sort(key=lambda x: x['metrics']['total_time'])
        elif priority == 'distance':
            # Quãng đường ngắn nhất
            routes_data.sort(key=lambda x: x['metrics']['total_distance'])
        elif priority == 'cost':
            # Giá rẻ nhất (chi phí tổng)
            routes_data.sort(key=lambda x: x['metrics'].get('total_cost', 0))
        elif priority == 'charges' or priority == 'stops':
            # Ít lần sạc nhất
            routes_data.sort(key=lambda x: x['metrics']['num_charges'])
        else:
            # Mặc định: theo thời gian
            routes_data.sort(key=lambda x: x['metrics']['total_time'])
        
        # === LOGIC MỚI: GÁN TÊN VÀ MÀU SẮC SAU KHI ĐÃ BIẾT AI LÀ NHẤT ===
        for i, route in enumerate(routes_data):
            if i == 0:
                # Tuyến đầu tiên sau khi sort chính là TUYẾN TỐI ƯU
                route['name'] = 'Lộ trình tối ưu'
                route['color'] = '#3b82f6'  # Màu Xanh (Blue)
                route['is_best'] = True
                route['z_index'] = 1000     # Vẽ lên trên cùng
            else:
                # Các tuyến còn lại là THAY THẾ
                route['name'] = f'Lộ trình thay thế {i}' if len(routes_data) > 2 else 'Lộ trình thay thế'
                route['color'] = '#94a3b8'  # Màu Xám (Gray)
                route['is_best'] = False
                route['z_index'] = 1        # Vẽ chìm xuống dưới
        
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
            # Lấy travel_events từ metrics (đã tính toán trong a_star_ev_routing)
            route_travel_events = r['metrics'].get('travel_events', [])
            add_route_to_map(
                m, 
                r['coords'], 
                r['stops'], 
                travel_events=route_travel_events,  # Truyền travel_events để vẽ lên bản đồ
                color=r['color'], 
                label=r['name']
            )
        
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
                'segments': r.get('segments', []),
                'travel_events': r['metrics'].get('travel_events', []),
                'filter_feedback': r.get('filter_feedback', [])  # Phản hồi hiệu quả bộ lọc
            } for r in routes_data]
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