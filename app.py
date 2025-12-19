import json
import polyline
import requests
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from flask import Flask, render_template, request, jsonify, send_from_directory
import heapq
import math
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
# NOTE: Folium/MarkerCluster and Matplotlib removed - server now returns JSON, rendering is client-side
# -----------------------

# Biến lưu trạng thái log hiện tại để gửi xuống Client
CURRENT_PROCESSING_LOG = "Đang khởi tạo..."

def log_status(message):
    """Hàm helper: Vừa in ra terminal, vừa lưu log để Client đọc"""
    global CURRENT_PROCESSING_LOG
    CURRENT_PROCESSING_LOG = message
    print(f"👉 [SERVER LOG]: {message}") # In ra terminal như cũ

# ==============================================================================
#  CACHING CHO OSRM REQUESTS
# ==============================================================================
ROUTE_CACHE = {}  # Cache cho các request OSRM
CACHE_TTL = 3600  # Cache 1 giờ

# ==============================================================================
#  CẤU HÌNH GOONG API (API chính cho Việt Nam)
# ==============================================================================
GOONG_API_KEY = "wbkIMwoWVcgXzbmC3fA4GsktVFZXVHczVRwdkV7W"  # Routing API
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

    # === DATA SANITIZATION: ensure lat/lon are floats and drop invalid entries ===
    def sanitize_geo(items, name):
        cleaned = []
        removed = 0
        for it in items:
            try:
                lat = it.get('lat') if it.get('lat') is not None else it.get('latitude')
                lon = it.get('lon') if it.get('lon') is not None else it.get('longitude')
                if lat is None or lon is None:
                    removed += 1
                    continue
                latf = float(lat)
                lonf = float(lon)
                it['lat'] = latf
                it['lon'] = lonf
                cleaned.append(it)
            except Exception:
                removed += 1
                continue
        if removed > 0:
            print(f"⚠️ Sanitized {removed} invalid {name} entries (removed)")
        return cleaned

    stations = sanitize_geo(stations, 'stations')
    ferries = sanitize_geo(ferries, 'ferries')
    highway_stations = sanitize_geo(highway_stations, 'highway_stations')
    toll_stations = sanitize_geo(toll_stations, 'toll_stations')

    # Helper: safe boolean parser for JSON inputs
    def str2bool(v):
        """Safely parse boolean-like inputs from JSON (accepts 'true'/'false' strings, 1/0, etc.)."""
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip().lower() in ('1', 'true', 't', 'yes', 'y')
        if isinstance(v, (int, float)):
            return v != 0
        return bool(v)

    # Additionally load highway nodes JSON (offline) - sanitized earlier if present
    try:
        with open('ev_database.highways.json', 'r', encoding='utf-8') as hf:
            highway_nodes = json.load(hf)
    except Exception:
        highway_nodes = highway_nodes if 'highway_nodes' in globals() else []
    # sanitize highway_nodes as well
    if highway_nodes:
        highway_nodes = sanitize_geo(highway_nodes, 'highway_nodes')


    # Additionally load highway node dataset from JSON file (offline data integrated by user)
    try:
        with open('ev_database.highways.json', 'r', encoding='utf-8') as hf:
            highway_nodes = json.load(hf)
        print(f" Đã tải {len(highway_nodes)} điểm cao tốc (from ev_database.highways.json)")
        if len(highway_nodes) > 0:
            hw_coords = np.array([[float(h.get('lat')), float(h.get('lon'))] for h in highway_nodes if h.get('lat') and h.get('lon')])
            highway_tree = cKDTree(hw_coords)
            print(" Đã khởi tạo KD-Tree cho cao tốc")
        else:
            highway_nodes = []
            highway_tree = None
    except Exception as e:
        print(f"⚠️ Không thể tải ev_database.highways.json: {e}")
        highway_nodes = []
        highway_tree = None

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

    # TẠO KD-TREE CHO TRẠM SẠC (TỐI ƯU TÌM KIẾM) - chỉ dùng station có lat/lon hợp lệ
    if len(stations) > 0:
        valid_station_coords = []
        for s in stations:
            try:
                if s.get('lat') is None or s.get('lon') is None:
                    continue
                valid_station_coords.append([float(s['lat']), float(s['lon'])])
            except Exception:
                continue
        if valid_station_coords:
            station_coords = np.array(valid_station_coords, dtype=float)
            station_tree = cKDTree(station_coords)
            print(" Đã khởi tạo KD-Tree cho trạm sạc")
        else:
            station_tree = None
            print(" Không có trạm sạc hợp lệ để tạo KD-Tree")
    else:
        station_tree = None
        print(" Không có trạm sạc để tạo KD-Tree")

    # TẠO KD-TREE CHO CAO TỐC (nếu có dữ liệu ev_database.highways.json)
    if highway_nodes and len(highway_nodes) > 0:
        try:
            hw_coords = np.array([[h['lat'], h['lon']] for h in highway_nodes], dtype=float)
            highway_tree = cKDTree(hw_coords)
            print(f" Đã khởi tạo KD-Tree cho cao tốc ({len(hw_coords)} nodes)")
        except Exception as e:
            print(f"⚠️ Lỗi tạo KD-Tree cho cao tốc: {e}")
            highway_tree = None
    else:
        highway_tree = None

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

# Backtrack / sliding window configuration (km / minutes)
EV_CONFIG['backtrack_window_km'] = 10.0
EV_CONFIG['backtrack_margin_min'] = 5.0  # allow backward moves that reduce f_cost by this margin (min)

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
    """Trả về icon Google Material Symbols tương ứng hành động"""
    m_type = str(m_type).lower()
    modifier = str(modifier).lower() if modifier else ""
    
    if m_type == "arrive": return "flag"
    if m_type == "depart": return "trip_origin"
    
    if "rotary" in m_type or "roundabout" in m_type:
        return "roundabout_right" if "right" in modifier else "roundabout_left"
        
    if "uturn" in m_type or "uturn" in modifier:
        return "u_turn_right" if "right" in modifier else "u_turn_left"
        
    if "left" in modifier: 
        if "sharp" in modifier: return "turn_sharp_left"
        if "slight" in modifier: return "turn_slight_left"
        return "turn_left"
        
    if "right" in modifier:
        if "sharp" in modifier: return "turn_sharp_right"
        if "slight" in modifier: return "turn_slight_right"
        return "turn_right"
        
    if m_type == "continue" or "straight" in modifier: 
        return "straight"
        
    if m_type == "merge": return "merge"
    if m_type == "fork": return "alt_route"
    if m_type == "off ramp": return "ramp_right"
    if m_type == "on ramp": return "ramp_right"
    
    return "navigation"

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

# Folium-based map generation functions removed (migrated to client-side rendering with GoongJS).
# Server does calculation only; map HTML generation no longer performed on backend.

# Folium marker snippet removed — server no longer builds Folium maps. Nearby stations are returned in JSON and rendered client-side with GoongJS/Leaflet.            
# ==============================================================================
#  HÀM LẤY ROUTE TỪ GOONG API (API CHÍNH)
# ==============================================================================
def get_route_from_goong(start_coords, end_coords, vehicle='car', user_filters=None, force_vn_waypoint=False):
    """
    Lấy route từ Goong Direction API - Tối ưu cho Việt Nam.
    
    ✅ ĐÃ NÂNG CẤP: Yêu cầu geometry chi tiết (alternatives=true, steps=true)
    để đường đi bám sát thực tế, không bị gãy khúc.
    
    Goong API được thiết kế riêng cho hạ tầng giao thông Việt Nam,
    tự động xử lý các đường nội địa mà không cần logic chunking/anchors.
    
    Args:
        start_coords: Tuple (lat, lon) điểm bắt đầu
        end_coords: Tuple (lat, lon) điểm kết thúc
        vehicle: Phương tiện ('car', 'bike', 'taxi')
        user_filters: Dict các tùy chọn tránh (tolls, highways, ferries)
        
    Returns:
        list: Danh sách routes với geometry chi tiết hoặc [] nếu thất bại
    """
    try:
        # Goong API yêu cầu format: lat,lon (Vĩ độ, Kinh độ)
        origin = f"{start_coords[0]},{start_coords[1]}"
        destination = f"{end_coords[0]},{end_coords[1]}"
        
        params = {
            "origin": origin,
            "destination": destination,
            "vehicle": vehicle,
            "api_key": GOONG_API_KEY,
            
            "overview": "full", 

            "alternatives": "true",
            "steps": "true"
        }

        # --- FIX: FORCE VIETNAM ROUTE (Tránh đi qua Lào/Campuchia) ---
        # Nguyên nhân: Với lộ trình Bắc - Nam dài, thuật toán thường chọn đường qua Lào/Cam vì ngắn hơn.
        # Giải pháp: Nếu lộ trình đi qua miền Trung (vượt qua vĩ độ 16), chèn điểm neo tại Đà Nẵng.
        
        lat_min = min(start_coords[0], end_coords[0])
        lat_max = max(start_coords[0], end_coords[0])
        
        # Kiểm tra nếu lộ trình băng qua miền Trung (Vĩ độ 16 - Đà Nẵng)
        # Và khoảng cách đủ xa (> 4 độ vĩ tuyến ~ 450km) để tránh ảnh hưởng lộ trình ngắn
        if lat_min < 15.0 and lat_max > 17.0:
            print("   🛡️ Phát hiện lộ trình Bắc - Nam dài: Thêm Waypoint Đà Nẵng để giữ lộ trình trong Việt Nam.")
            # Tọa độ Đà Nẵng (Cầu Rồng): 16.06104, 108.22690
            # Goong API hỗ trợ tham số 'waypoints'
            params['waypoints'] = "16.06104,108.22690"

        # If user provided filters (avoid), pass them to Goong API when supported.
        # Note: Goong's API may use a different keyword (e.g., 'avoid' or 'exclude') and value names.
        # We'll attempt a sensible mapping and keep it safe: if user_filters present, add 'avoid' param as pipe-separated list.
        if user_filters:
            avoids = []
            if user_filters.get('avoid_tolls'):
                avoids.append('toll')
            if user_filters.get('avoid_highways'):
                avoids.append('motorway')
            if user_filters.get('avoid_ferries'):
                avoids.append('ferry')
            if avoids:
                params['avoid'] = '|'.join(avoids)
        
        # Force a Vietnam waypoint if requested (keeps route inside VN borders)
        if force_vn_waypoint:
            params['waypoints'] = "16.06104,108.22690"
            print("   🛡️ Forcing Vietnam waypoint for Goong request")

        print(f"🗺️ Đang gọi Goong Direction API...")
        print(f"   📍 Origin: {origin}")
        print(f"   📍 Destination: {destination}")
        
        response = requests.get(GOONG_DIRECTION_URL, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            
            # ✅ FIX: Goong API có thể không trả về 'status' hoặc trả về 'geocoded_waypoints'
            # Chỉ cần kiểm tra có 'routes' và không rỗng là thành công
            if data.get("routes") and len(data["routes"]) > 0:
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
                # Debug: In ra các keys để biết tại sao thất bại
                print(f"⚠️ Goong Response Keys: {list(data.keys())}")
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
    # Build user_filters from options (defaults to False)
    user_filters = {}
    if options:
        user_filters = {
            'avoid_tolls': bool(options.get('avoid_tolls', False)),
            'avoid_highways': bool(options.get('avoid_highways', False)),
            'avoid_ferries': bool(options.get('avoid_ferries', False)),
            'avoid_highway_stops': bool(options.get('avoid_highway_stops', False)),
        }
    if any(user_filters.values()):
        print(f"🔒 Applying user_filters to routing: {user_filters}")

    # Helper: check if route (decoded coords) appears to use a motorway/highway segment
    def route_has_highway_segment(route_coords, max_distance_km=0.05):
        """Return True if any sampled route point is within max_distance_km of a highway node.
        Uses `highway_tree` (JSON nodes) when available, otherwise falls back to `highway_stations` KD-tree.
        """
        if not route_coords:
            return False
        # degrees bound approx, 1 deg ~ 111 km
        deg_bound = max(0.00001, max_distance_km / 111.0)
        # sample to reduce queries
        step = max(1, len(route_coords) // 200)
        sampled = route_coords[::step]
        try:
            if 'highway_tree' in globals() and highway_tree is not None and len(highway_nodes) > 0:
                for coord in sampled:
                    # coord is (lat, lon)
                    try:
                        dist, idx = highway_tree.query((coord[0], coord[1]), k=1, distance_upper_bound=deg_bound)
                        if dist != float('inf'):
                            return True
                    except Exception:
                        continue
            # fallback to highway_stations loaded from MongoDB
            if 'highway_stations' in globals() and highway_stations and len(highway_stations) > 0:
                for coord in sampled:
                    for hw in highway_stations:
                        try:
                            d = geodesic((coord[0], coord[1]), (hw['lat'], hw['lon'])).km
                            if d <= max_distance_km:
                                return True
                        except Exception:
                            continue
        except Exception:
            return False
        return False

    routes = get_route_from_goong(start_coords, end_coords, vehicle='car', user_filters=user_filters)
    
    if routes:
        print(f"✅ Goong thành công! Nhận được {len(routes)} route(s)")
        # If user requested avoids but Goong returned routes, validate quickly whether returned
        # route contains the avoided features; if it does, try Mapbox with excludes as a fallback.
        try:
            if any(user_filters.values()):
                # ✅ FIX: Kiểm tra TẤT CẢ các route Goong trả về (vì đã bật alternatives=true)
                # Tìm route đầu tiên không vi phạm filters
                best_valid_route_idx = -1
                
                for idx, route in enumerate(routes):
                    # Decode geometry
                    coords = []
                    geom = route.get('geometry')
                    if isinstance(geom, str) and geom:
                        try:
                            coords = polyline.decode(geom)
                        except Exception:
                            coords = []
                    elif isinstance(geom, list) and geom:
                        coords = geom
                    
                    # Check violations
                    is_violation = False
                    if coords:
                        if user_filters.get('avoid_tolls') and find_toll_stations_on_route(coords, max_distance_km=0.1):
                            is_violation = True
                        if not is_violation and user_filters.get('avoid_ferries') and find_ferries_on_route(coords, max_distance_km=0.2):
                            is_violation = True
                        # Check highway-related violations: prefer node-based detection using highway_tree or fall back to station detection
                        if not is_violation and (user_filters.get('avoid_highways') or user_filters.get('avoid_highway_stops')):
                            try:
                                if route_has_highway_segment(coords, max_distance_km=0.05):
                                    is_violation = True
                                elif find_highway_stations_on_route(coords, max_distance_km=0.1):
                                    # fallback conservative check
                                    is_violation = True
                            except Exception:
                                # fallback to station-based detection
                                if find_highway_stations_on_route(coords, max_distance_km=0.1):
                                    is_violation = True
                    
                    if not is_violation:
                        best_valid_route_idx = idx
                        break
                
                # Nếu tìm thấy route hợp lệ trong danh sách Goong trả về
                if best_valid_route_idx != -1:
                    if best_valid_route_idx > 0:
                        print(f"✅ Goong Route #{best_valid_route_idx} tuân thủ filters! Đưa lên đầu danh sách.")
                        # Đưa route tốt nhất lên đầu
                        routes.insert(0, routes.pop(best_valid_route_idx))
                else:
                    # Nếu TẤT CẢ route của Goong đều vi phạm -> Mới fallback sang Mapbox
                    print("⚠️ Tất cả Goong routes đều vi phạm filters — attempting Mapbox fallback...")
                    mapbox_routes = get_route_from_mapbox(start_coords, end_coords, user_filters=user_filters)
                    if mapbox_routes:
                        print("✅ Mapbox found an alternative that respects filters — using Mapbox result")
                        ROUTE_CACHE[cache_key] = {'data': mapbox_routes, 'time': time.time()}
                        return mapbox_routes, None
                    else:
                        print("⚠️ Mapbox could not find a filter-respecting alternative — keeping Goong route")
        except Exception as e:
            print(f"⚠️ Warning while validating filters against Goong route: {e}")
        # Validate that the route stays within Vietnam; if not, request with forced VN waypoint
        def route_outside_vietnam(route):
            coords = []
            geom = route.get('geometry')
            if isinstance(geom, str) and geom:
                try:
                    coords = polyline.decode(geom)
                except Exception:
                    coords = []
            elif isinstance(geom, list) and geom:
                coords = geom
            # Also attempt steps if no geometry
            if not coords and route.get('steps'):
                for s in route['steps']:
                    try:
                        coords.extend(polyline.decode(s.get('geometry', '')))
                    except Exception:
                        continue
            if not coords:
                return False
            # Vietnam bounding box (conservative mainland): lat 8..23.6 lon 102..110.6
            for lat, lon in coords:
                if lat < 8.0 or lat > 23.6 or lon < 102.0 or lon > 110.6:
                    return True
            return False

        primary_route = routes[0] if routes else None
        if primary_route and route_outside_vietnam(primary_route):
            print("⚠️ Route appears to cross outside Vietnam; retrying with forced VN waypoint...")
            forced = get_route_from_goong(start_coords, end_coords, vehicle='car', user_filters=user_filters, force_vn_waypoint=True)
            if forced:
                print("✅ Forced VN waypoint route obtained; using it")
                ROUTE_CACHE[cache_key] = {'data': forced, 'time': time.time()}
                return forced, None
            else:
                print("⚠️ Forced VN waypoint did not yield alternate route; keeping original route")

        # Lưu cache
        ROUTE_CACHE[cache_key] = {'data': routes, 'time': time.time()}
        return routes, None
    
    # === 5. FALLBACK SANG MAPBOX ===
    print(f"\n⚠️ Goong thất bại, chuyển sang Mapbox backup...")
    mapbox_routes = get_route_from_mapbox(start_coords, end_coords, user_filters=user_filters)
    
    if mapbox_routes:
        print(f"✅ Mapbox backup thành công!")
        
        def route_outside_vietnam(route):
            coords = []
            geom = route.get('geometry')
            if isinstance(geom, str) and geom:
                try:
                    coords = polyline.decode(geom)
                except Exception:
                    coords = []
            elif isinstance(geom, list) and geom:
                coords = geom
            if not coords and route.get('steps'):
                for s in route['steps']:
                    try:
                        coords.extend(polyline.decode(s.get('geometry', '')))
                    except Exception:
                        continue
            if not coords:
                return False
            for lat, lon in coords:
                if lat < 8.0 or lat > 23.6 or lon < 102.0 or lon > 110.6:
                    return True
            return False

        primary_route = mapbox_routes[0]
        if primary_route and route_outside_vietnam(primary_route):
            print("⚠️ Mapbox route crosses outside Vietnam — retrying with forced VN waypoint...")
            forced_mapbox = get_route_from_mapbox(start_coords, end_coords, user_filters=user_filters, force_vn_waypoint=True)
            if forced_mapbox:
                print("✅ Forced Mapbox VN waypoint route obtained; using it")
                ROUTE_CACHE[cache_key] = {'data': forced_mapbox, 'time': time.time()}
                return forced_mapbox, None
            else:
                print("⚠️ Forced Mapbox waypoint did not yield alternate route; keeping Mapbox route")

        ROUTE_CACHE[cache_key] = {'data': mapbox_routes, 'time': time.time()}
        return mapbox_routes, None
    else:
        if user_filters.get('avoid_highways') or user_filters.get('avoid_tolls') or user_filters.get('avoid_ferries'):
            print("⚠️ Mapbox returned no routes with excludes — retrying without excludes and marking as violated (soft fallback)")
            retry_routes = get_route_from_mapbox(start_coords, end_coords, user_filters=None)
            if retry_routes:
                for rr in retry_routes:
                    rr['violated_filters'] = [k for k, v in user_filters.items() if v]
                    ff = rr.get('filter_feedback', [])
                    ff.append({'type': 'warning', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">warning</span>', 'text': 'Không thể tránh hoàn bộ các tuỳ chọn lọc'})
                    rr['filter_feedback'] = ff
                print("✅ Soft fallback obtained routes (marked as violated)")
                ROUTE_CACHE[cache_key] = {'data': retry_routes, 'time': time.time()}
                return retry_routes, None
            else:
                print("⚠️ Soft fallback also returned no routes — giving up on Mapbox backup")
    
    # === 6. TẤT CẢ ĐỀU THẤT BẠI ===
    print("=" * 50)
    print("❌ THẤT BẠI: Không thể lấy được lộ trình từ cả Goong và Mapbox")
    print(f"📍 Từ: {start_coords}")
    print(f"📍 Đến: {end_coords}")
    print(f"📏 Khoảng cách: {distance_km:.1f} km")
    print("=" * 50)
    
    return [], "Không thể kết nối đến server định tuyến (Goong/Mapbox)"

def get_route_from_mapbox(start_coords, end_coords, user_filters=None, force_vn_waypoint=False):
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
        
        # Decide whether to insert a Vietnam waypoint to avoid cross-border shortcuts
        # If the route spans southern to northern Vietnam, we add Đà Nẵng waypoint.
        lat_min = min(start_coords[0], end_coords[0])
        lat_max = max(start_coords[0], end_coords[0])

        if force_vn_waypoint or (lat_min < 15.0 and lat_max > 17.0):
            print("   🛡️ Mapbox: Detected long VN route or forced waypoint — inserting Đà Nẵng waypoint to keep route inside Vietnam.")
            waypoint_str = "108.22690,16.06104"  # lon,lat for Mapbox
            url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{start_str};{waypoint_str};{end_str}"
        else:
            url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{start_str};{end_str}"

        params = {
            "access_token": MAPBOX_ACCESS_TOKEN,
            "geometries": "polyline",  # Lấy encoded polyline giống OSRM
            "overview": "full",
            "steps": "true",           # Lấy chỉ dẫn đường đi
            "alternatives": "false"    # Chỉ lấy 1 đường tốt nhất
        }

        # Support exclude param for Mapbox to avoid motorway/toll/ferry when user requests it
        if user_filters:
            excludes = []
            if user_filters.get('avoid_highways'):
                excludes.append('motorway')
            if user_filters.get('avoid_tolls'):
                excludes.append('toll')
            if user_filters.get('avoid_ferries'):
                excludes.append('ferry')
            if excludes:
                params['exclude'] = ','.join(excludes)
                # Ask for alternatives when user requests avoid so we can pick best non-offending route
                params['alternatives'] = 'true'
        
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

def decode_full_route_geometry(route_steps):
    """
    Tái tạo toàn bộ lộ trình chi tiết từ các bước (steps).
    Giúp đường đi mượt mà, bám sát mặt đường thực tế thay vì bị gãy khúc.
    """
    full_coords = []
    for step in route_steps:
        # Giải mã geometry của từng bước nhỏ (độ chính xác cao)
        step_coords = polyline.decode(step['geometry'])
        
        # Nối vào danh sách chính
        # Lưu ý: Điểm cuối step trước thường trùng điểm đầu step sau, cần xử lý để tránh trùng
        if full_coords and step_coords:
            # Nếu điểm đầu step này trùng điểm cuối danh sách -> bỏ qua 1 điểm
            if full_coords[-1] == step_coords[0]:
                full_coords.extend(step_coords[1:])
            else:
                full_coords.extend(step_coords)
        else:
            full_coords.extend(step_coords)
            
    return full_coords

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
        try:
            toll_pos = (float(toll['lat']), float(toll['lon']))
        except Exception:
            # Bad toll data, skip
            continue
        
        # --- Khử trùng lặp địa lý (2km) ---
        is_duplicate = False
        for added_pos in added_toll_coords:
            try:
                ddup = geodesic(toll_pos, added_pos).km
                if ddup < 2.0:
                    is_duplicate = True
                    break
            except Exception:
                continue
        if is_duplicate:
            continue

        # --- Tính khoảng cách ---
        min_dist = float('inf')
        closest_idx = 0
        any_success = False
        # Tìm điểm gần nhất trên đường
        for i, route_pt in enumerate(sampled_coords):
            try:
                d = geodesic(toll_pos, route_pt).km
                any_success = True
                if d < min_dist:
                    min_dist = d
                    closest_idx = i
            except Exception:
                continue
        # Nếu không thể tính khoảng cách vì dữ liệu hỏng, skip
        if not any_success:
            continue
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
        try:
            ferry_pos = (float(ferry['lat']), float(ferry['lon']))
        except Exception:
            continue
        
        # Khử trùng lặp 3km
        is_dup = False
        for added in added_ferry_coords:
            try:
                if geodesic(ferry_pos, added).km < 3.0:
                    is_dup = True
                    break
            except Exception:
                continue
        if is_dup:
            continue
        
        # Tìm khoảng cách nhỏ nhất
        min_dist = float('inf')
        closest_idx = 0
        any_success = False
        for i, pt in enumerate(sampled_coords):
            try:
                d = geodesic(ferry_pos, pt).km
                any_success = True
                if d < min_dist:
                    min_dist = d
                    closest_idx = i
            except Exception:
                continue
        if not any_success:
            continue
        
        # Phà có thể lệch toạ độ nhiều hơn trạm thu phí, để 200m (0.2)
        if min_dist <= max_distance_km:
            # Lấy phí
            fee = ferry.get('fee', 20000)
            if isinstance(fee, str):
                import re
                nums = re.findall(r'[\d,.]+', fee.replace(',', ''))
                fee = int(float(nums[0])) if nums else 20000
            
            # Ước lượng km trên lộ trình (defensive)
            try:
                route_len_km = geodesic(route_coords[0], route_coords[-1]).km
            except Exception:
                route_len_km = 0
            km_on_route = (closest_idx / max(1, len(sampled_coords))) * route_len_km * 1.3

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
        any_success = False
        for i, coord in enumerate(sampled_coords):
            try:
                dist = geodesic(station_pos, coord).km
                any_success = True
                if dist < min_distance:
                    min_distance = dist
                    try:
                        route_len_km = geodesic(sampled_coords[0], sampled_coords[-1]).km
                    except Exception:
                        route_len_km = 0
                    closest_km = (i / max(1, len(sampled_coords))) * route_len_km * 1.3
            except Exception:
                continue
        if not any_success:
            continue
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

def get_stations_along_route(route_coords, route_km_cumulative, buffer_km=20.0):
    """
    Tìm tất cả trạm sạc trong bán kính buffer_km dọc theo lộ trình.
    
    Chiến lược "Vây lưới" (Corridor Search):
    1. Lấy mẫu các điểm trên route (mỗi ~10km để tối ưu hiệu năng)
    2. Query KD-Tree tìm trạm gần mỗi điểm mẫu
    3. Project mỗi trạm lên route để tính km_position
    
    UPGRADED:
    - buffer_km: 20km (tối ưu hóa tìm kiếm)
    - sample_interval_km: 10km (giảm số lần query KD-Tree)
    - Clustering: Gom nhóm các trạm gần nhau (<3km) để giảm không gian tìm kiếm A*
    
    Args:
        route_coords: Danh sách tọa độ [(lat, lon), ...]
        route_km_cumulative: Danh sách km tích lũy tại mỗi điểm [0, 0.5, 1.2, ...]
        buffer_km: Bán kính tìm kiếm (km) - default 20km
        
    Returns:
        list: Danh sách trạm đã được sắp xếp theo km_position
    """
    if station_tree is None or len(stations) == 0:
        print("⚠️ Không có dữ liệu trạm sạc!")
        return []
    
    # 1. LẤY MẪU ROUTE (Tối ưu hóa: Lấy mẫu thưa hơn)
    total_route_km = route_km_cumulative[-1] if route_km_cumulative else 0
    
    # Tăng khoảng cách lấy mẫu cho các tuyến dài (Adaptive sampling)
    # Mục tiêu: giảm số lần KD-Tree query khi route rất dài
    if total_route_km > 1500:
        sample_interval_km = 50.0  # Rất dài -> lấy mẫu thưa (50 km)
    elif total_route_km > 500:
        sample_interval_km = 10.0  # Dài -> lấy mẫu ~10 km
    else:
        sample_interval_km = 0.5  # Bình thường -> lấy mẫu ~3 km
    
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
            # Tăng số lượng neighbors (k) lên 50 vì lấy mẫu thưa hơn
            distances, indices = station_tree.query(coord, k=min(50, len(stations)))
            
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
    
    # 5. CLUSTERING (Gom nhóm trạm) - Tối ưu hóa A*
    filtered_candidates = []
    if candidates:
        last_added = candidates[0]
        filtered_candidates.append(last_added)
        
        for current in candidates[1:]:
            # Nếu trạm này cách trạm trước < 3km
            dist_between = abs(current['km_position'] - last_added['km_position'])
            
            if dist_between < 3.0: 
                # Chỉ giữ lại trạm tốt hơn (công suất lớn hơn) trong cùng 1 cụm
                if current['power'] > last_added['power']:
                    filtered_candidates.pop()
                    filtered_candidates.append(current)
                    last_added = current
                # Nếu không thì bỏ qua current
            else:
                filtered_candidates.append(current)
                last_added = current
                
    print(f"🔍 Tìm thấy {len(candidates)} trạm, sau khi gom nhóm còn {len(filtered_candidates)} trạm")
    
    return filtered_candidates


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


def a_star_ev_routing(start_coords, end_coords, route_coords, start_battery, config, priority='time', user_filters=None):
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
    candidates = get_stations_along_route(route_coords, route_km_cumulative, buffer_km=25.0)
    
    # --- EMERGENCY INJECTION: Nếu pin thấp, tìm trạm gần nhất bất kể lộ trình ---
    SAFE_THRESHOLD = config.get('min_battery', 20)
    if start_battery < SAFE_THRESHOLD + 15:
        print(f"⚠️ Pin thấp ({start_battery}%) - Kích hoạt chế độ tìm kiếm khẩn cấp (Emergency Injection)")
        if station_tree is not None and len(stations) > 0:
            try:
                # Tìm 5 trạm gần nhất trong bán kính 15km
                distances, indices = station_tree.query(start_coords, k=min(5, len(stations)))
                
                # Đảm bảo indices là list
                if not isinstance(indices, (list, np.ndarray)):
                    indices = [indices]
                    distances = [distances]
                
                existing_station_indices = {c['station_idx'] for c in candidates}
                
                for dist_deg, idx in zip(distances, indices):
                    if idx in existing_station_indices:
                        continue
                        
                    station = stations[idx]
                    station_pos = (station['lat'], station['lon'])
                    dist_km = geodesic(start_coords, station_pos).km
                    
                    if dist_km > 15.0: # Bỏ qua nếu quá xa
                        continue
                        
                    # Project lên route để có km_position (dù có thể là 0)
                    # Tìm điểm gần nhất trên route
                    min_dist_to_route = float('inf')
                    best_route_idx = 0
                    
                    # Chỉ quét 100 điểm đầu tiên của route để tiết kiệm (vì đang ở gần start)
                    scan_limit = min(100, len(route_coords))
                    for i in range(scan_limit):
                        d = geodesic(station_pos, route_coords[i]).km
                        if d < min_dist_to_route:
                            min_dist_to_route = d
                            best_route_idx = i
                    
                    km_position = route_km_cumulative[best_route_idx]
                    
                    # Thêm vào candidates
                    print(f"   🚑 Inject trạm khẩn cấp: {station.get('name')} (cách {dist_km:.1f}km)")
                    candidates.append({
                        'station': station,
                        'station_idx': idx,
                        'km_position': km_position,
                        'detour_km': min_dist_to_route, # Dùng khoảng cách tới route làm detour
                        'route_idx': best_route_idx,
                        'power': station.get('power', 150),
                        'type': 'station',
                        'is_emergency': True # Đánh dấu
                    })
                    
            except Exception as e:
                print(f"⚠️ Lỗi Emergency Injection: {e}")
                
    # Sắp xếp lại candidates theo km_position
    candidates.sort(key=lambda x: x['km_position'])
    
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
        return build_direct_route_result(route_coords, route_km_cumulative, start_battery, config, user_filters=user_filters), None
    
    # Trường hợp 2: Không có trạm sạc nào
    if len(candidates) == 0:
        # No stations near route: compute failure point metadata and return concise infeasibility
        farthest_reachable_km = max_range_km * 0.95
        failure_idx = 0
        for i, km in enumerate(route_km_cumulative):
            if km <= farthest_reachable_km:
                failure_idx = i
            else:
                break
        failure_point = route_coords[failure_idx]
        failure_km = route_km_cumulative[failure_idx]
        remaining_km = total_route_km - failure_km

        nearest_station = None
        if station_tree is not None and len(stations) > 0:
            try:
                distances, indices = station_tree.query(failure_point, k=1)
                idx = indices if isinstance(indices, (int, np.integer)) else indices[0]
                st = stations[idx]
                dist_to_st = geodesic(failure_point, (st['lat'], st['lon'])).km
                nearest_station = {
                    'name': st.get('name', 'Trạm sạc'),
                    'distance_km': round(dist_to_st, 1),
                    'address': st.get('address', 'Không xác định')
                }
            except Exception:
                nearest_station = None

        error_info = {
            "error": "infeasible",
            "error_type": "battery_depleted",
            "severity": "critical",
            "message": "Không đủ năng lượng để hoàn thành hành trình",
            "analysis": {
                "total_dist": round(total_route_km, 1),
                "max_range": round(max_range_km, 1),
                "traveled": round(failure_km, 1),
                "missing": round(remaining_km, 1),
                "battery_start": start_battery,
                "fail_coords": list(failure_point)
            },
            "nearest_rescue": nearest_station,
            "suggestion": "Không tìm thấy trạm sạc trên hành trình này"
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
        
        # === EXPAND: Thử đi đến các node phía trước và các node lân cận (pruned candidate set) ===
        candidate_indices = build_candidate_indices(current.node_id, nodes, config)
        for next_idx in candidate_indices:
            next_node = nodes[next_idx]
            
            # Tính khoảng cách đến next_node (dùng giá trị tuyệt đối để hỗ trợ backtrack)
            dist_on_route = abs(next_node['km_position'] - current_node['km_position'])
            detour_dist = next_node.get('detour_km', 0) * 2  # Đi vào + ra
            total_dist = (dist_on_route + detour_dist) * SAFETY_FACTOR
            
            # Tính pin tiêu thụ
            battery_consumed = calculate_battery_consumption(total_dist, config)
            battery_remaining = current.battery - battery_consumed
            
            # === SOFT CONSTRAINT & CHARGING LOGIC ===
            MIN_BATTERY = config.get('min_battery', 20)
            DEAD_BATTERY = 5.0
            penalty_cost = 0
            
            # 1. XỬ LÝ SẠC (Nếu current là station)
            if current_node['type'] == 'station' and current.battery < 100:
                # Kích hoạt sạc nếu pin thấp hoặc chiến lược tối ưu
                should_charge = (battery_remaining < MIN_BATTERY) or (current.battery < 80)
                
                if should_charge:
                    # Use smart charging strategy to decide partial vs full charge
                    # Default safe value
                    optimal_target = 100.0
                    try:
                        remaining_nodes_indices = range(current.node_id + 1, len(nodes))
                        remaining_coords = [nodes[i]['coords'] for i in remaining_nodes_indices]

                        if remaining_coords:
                            val = calculate_optimal_charge_target(
                                remaining_coords,
                                current_node.get('station'),
                                current.battery,
                                config
                            )
                            # Coerce to float safely
                            if val is not None:
                                try:
                                    optimal_target = float(val)
                                except Exception:
                                    optimal_target = 100.0
                    except Exception as e:
                        # Fallback to conservative behavior if optimal calculation fails
                        print(f"⚠️ Warning: calculate_optimal_charge_target failed: {e}")
                        optimal_target = 100.0

                    # Ensure target is sufficient to reach next hop
                    min_needed_for_next_hop = battery_consumed + MIN_BATTERY
                    try:
                        target_battery = max(float(optimal_target), float(min_needed_for_next_hop))
                    except Exception:
                        target_battery = float(optimal_target or 100.0)

                    target_battery = min(100.0, target_battery)

                    # Only charge if it increases battery significantly
                    if target_battery > current.battery + 2:
                        charge_time = calculate_charge_time(
                            current.battery,
                            target_battery,
                            current_node.get('power', 150),
                            config
                        )

                        charge_added = target_battery - current.battery
                        energy_kwh = (charge_added / 100) * config['battery_capacity']

                        # Tính lại battery sau khi sạc và sau khi đi đến next_node
                        new_battery_after_charge = target_battery - battery_consumed

                        if new_battery_after_charge >= DEAD_BATTERY:
                            driving_time = (total_dist / AVG_SPEED_KMH) * 60
                            
                            # Penalty cho nhánh sạc (nếu vẫn < MIN)
                            charge_penalty = 0
                            if new_battery_after_charge < MIN_BATTERY:
                                charge_penalty = (MIN_BATTERY - new_battery_after_charge) * 20.0
                                
                            new_g_cost = current.g_cost + driving_time + charge_time + charge_penalty

                            # Heuristic (với trọng số 1.5 để tăng tốc cho route dài)
                            HEURISTIC_WEIGHT = 1.0
                            remaining_km = total_route_km - next_node['km_position']
                            h_cost = ((remaining_km / AVG_SPEED_KMH) * 60) * HEURISTIC_WEIGHT

                            new_node = AStarNode(
                                node_id=next_idx,
                                km_position=next_node['km_position'],
                                battery=new_battery_after_charge,
                                g_cost=new_g_cost,
                                h_cost=h_cost,
                                parent=current,
                                charged_at=current_node.get('station'),
                                charge_time=charge_time,
                                charge_amount=energy_kwh
                            )

                            heapq.heappush(pq, (new_node.f_cost, counter, new_node))
                            counter += 1
            
            # 2. XỬ LÝ ĐI TIẾP (Không sạc thêm)
            # Kiểm tra Hard Constraint (Dead Zone)
            if battery_remaining < DEAD_BATTERY:
                continue # Không thể đi tiếp
            
            # Kiểm tra Soft Constraint (Risk Zone)
            if battery_remaining < MIN_BATTERY:
                violation = MIN_BATTERY - battery_remaining
                # Trọng số phạt: 1% pin thiếu = 20 phút phạt
                penalty_cost = violation * 20.0
            
            # === BACKTRACK PRUNING: if this is a backward move, only accept if promising ===
            is_backward = next_node['km_position'] < current_node['km_position']
            if is_backward:
                driving_time = (total_dist / AVG_SPEED_KMH) * 60
                HEURISTIC_WEIGHT = 1.5
                remaining_km = total_route_km - next_node['km_position']
                h_cost = ((remaining_km / AVG_SPEED_KMH) * 60) * HEURISTIC_WEIGHT
                est_total = current.g_cost + driving_time + h_cost
                margin = config.get('backtrack_margin_min', 5.0)
                if est_total >= current.f_cost + margin:
                    # Skip non-promising backward move
                    continue

            # === CÓ THỂ ĐI ĐƯỢC ===
            driving_time = (total_dist / AVG_SPEED_KMH) * 60
            new_g_cost = current.g_cost + driving_time + penalty_cost

            # Heuristic (với trọng số 1.5 để tăng tốc cho route dài)
            HEURISTIC_WEIGHT = 1.5
            remaining_km = total_route_km - next_node['km_position']
            h_cost = ((remaining_km / AVG_SPEED_KMH) * 60) * HEURISTIC_WEIGHT

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
        # === 5. XỬ LÝ KHI KHÔNG TÌM THẤY ĐƯỜNG (INFEASIBLE) ===
        # Trả về object đơn giản, chỉ metadata để Frontend hiển thị Modal (không vẽ geometry)

        # Tính điểm hết pin dựa trên hiệu năng thực tế (95% buffer)
        farthest_reachable_km = max_range_km * 0.95
        failure_idx = 0
        for i, km in enumerate(route_km_cumulative):
            if km <= farthest_reachable_km:
                failure_idx = i
            else:
                break

        failure_point = route_coords[failure_idx]
        failure_km = route_km_cumulative[failure_idx]
        remaining_km = total_route_km - failure_km

        # Tìm trạm sạc gần nhất với điểm chết
        nearest_station = None
        if station_tree is not None and len(stations) > 0:
            try:
                distances, indices = station_tree.query(failure_point, k=1)
                idx = indices if isinstance(indices, (int, np.integer)) else indices[0]
                st = stations[idx]
                dist_to_st = geodesic(failure_point, (st['lat'], st['lon'])).km
                nearest_station = {
                    'name': st.get('name', 'Trạm sạc'),
                    'distance_km': round(dist_to_st, 1),
                    'address': st.get('address', 'Không xác định')
                }
            except Exception:
                nearest_station = None

        error_info = {
            "error": "infeasible",
            "error_type": "battery_depleted",
            "severity": "critical",
            "message": "Không đủ năng lượng để hoàn thành hành trình",
            "analysis": {
                "total_dist": round(total_route_km, 1),
                "max_range": round(max_range_km, 1),
                "traveled": round(failure_km, 1),
                "missing": round(remaining_km, 1),
                "battery_start": start_battery,
                "fail_coords": list(failure_point)
            },
            "nearest_rescue": nearest_station,
            "suggestion": "Dung lượng pin hoặc mức tiêu thụ của xe không cho phép đi quãng đường này mà không có trạm sạc phù hợp."
        }
        return None, error_info
    
    # Reconstruct path
    return reconstruct_astar_result(best_solution, nodes, route_coords, route_km_cumulative, config, user_filters=user_filters)


def reconstruct_astar_result(goal_node, nodes, route_coords, route_km_cumulative, config, user_filters=None):
    """
    Tái tạo kết quả từ A* goal_node.
    
    ✅ UPGRADED: Trả về geometry_coords đầy đủ để Frontend vẽ đường mượt mà
    """
    """
    Xây dựng kết quả từ A* solution path.
    
    Returns:
        dict: Kết quả với format giống hệt phiên bản cũ
        None: error (nếu có)
    """
    total_route_km = route_km_cumulative[-1]
    
    if user_filters is None:
        user_filters = {}
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
        
        is_violation = str2bool(user_filters.get('avoid_tolls', False))
        travel_events.append({
            'type': 'toll',
            'name': toll.get('name', 'Trạm thu phí'),
            'cost': fee,
            'km': toll_info.get('km_on_route', 0),
            'coords': [toll['lat'], toll['lon']],
            'details': {'highway': toll.get('highway', 'N/A')},
            'warning': is_violation
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
        is_violation = str2bool(user_filters.get('avoid_ferries', False))
        travel_events.append({
            'type': 'ferry',
            'name': ferry.get('name', 'Phà'),
            'cost': fee,
            'km': ferry_info.get('km_on_route', 0),
            'coords': [ferry['lat'], ferry['lon']],
            'details': {'crossing_time_min': ferry.get('crossing_time', 15)},
            'warning': is_violation
        })
    
    highway_stops_passed = []
    try:
        for hw_info in highways_on_route:
            station = hw_info.get('station', {})
            km_on_route = hw_info.get('km_on_route', 0)
            highway_stops_passed.append({
                'name': station.get('name', 'Trạm dừng'),
                'km': km_on_route
            })

            # Warn if user asked to avoid highways as road OR avoid highway stops specifically
            is_violation = str2bool(user_filters.get('avoid_highways', False) or user_filters.get('avoid_highway_stops', False))

            coords = None
            try:
                if station.get('lat') is not None and station.get('lon') is not None:
                    coords = [float(station.get('lat')), float(station.get('lon'))]
            except Exception:
                coords = None

            travel_events.append({
                'type': 'highway_stop',
                'name': station.get('name', 'Trạm dừng'),
                'km': km_on_route,
                'coords': coords,
                'details': {'amenities': station.get('amenities', [])},
                'warning': is_violation
            })
    except Exception as e:
        print(f"⚠️ Lỗi khi xử lý highway_stops_passed: {e}")

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
        
        # ✅ GEOMETRY: Trả về tọa độ chi tiết để Frontend vẽ đường mượt mà
        'geometry_coords': route_coords,  # Mảng [[lat, lon], ...] đầy đủ
        
        # A* metadata (để debug/bảo vệ đồ án)
        'algorithm': 'A* Graph-based',
        'nodes_explored': len([n for n in [goal_node] if n])
    }, None


def build_direct_route_result(route_coords, route_km_cumulative, start_battery, config, user_filters=None):
    """
    Xây dựng kết quả khi có thể đi thẳng không cần sạc.
    """
    # === Thiết lập mặc định cho user_filters để tránh AttributeError khi None được truyền vào ===
    if user_filters is None:
        user_filters = {}

    total_route_km = route_km_cumulative[-1]
    battery_consumed = calculate_battery_consumption(total_route_km, config)
    final_battery = start_battery - battery_consumed

    # Tính phí cầu đường, phà
    try:
        tolls_on_route = find_toll_stations_on_route(route_coords, max_distance_km=0.1)
    except Exception as e:
        print(f"⚠️ Lỗi khi tìm tolls_on_route: {e}")
        tolls_on_route = []
    try:
        ferries_on_route = find_ferries_on_route(route_coords, max_distance_km=0.2)
    except Exception as e:
        print(f"⚠️ Lỗi khi tìm ferries_on_route: {e}")
        ferries_on_route = []
    try:
        highways_on_route = find_highway_stations_on_route(route_coords, max_distance_km=0.1)
    except Exception as e:
        print(f"⚠️ Lỗi khi tìm highways_on_route: {e}")
        highways_on_route = []

    total_toll_cost = 0
    toll_stations_passed = []
    travel_events = []

    # Defensive: when computing toll/ferry/travel_events, isolate errors so route remains valid
    try:
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

            # === Gắn cờ warning nếu user yêu cầu tránh trạm thu phí ===
            is_violation = bool(user_filters.get('avoid_tolls', False))

            travel_events.append({
                'type': 'toll',
                'name': toll.get('name', 'Trạm thu phí'),
                'cost': fee,
                'km': toll_info.get('km_on_route', 0),
                'coords': [toll.get('lat'), toll.get('lon')] if toll.get('lat') and toll.get('lon') else None,
                'details': {'highway': toll.get('highway', 'N/A')},
                'warning': is_violation
            })
    except Exception as e:
        print(f"⚠️ Lỗi khi tính toán tolls (bỏ qua chi tiết): {e}")
        total_toll_cost = 0
        tolls_on_route = []

    total_ferry_cost = 0
    ferries_passed = []

    try:
        for ferry_info in ferries_on_route:
            ferry = ferry_info['ferry']
            fee = ferry_info.get('fee', 50000)
            if isinstance(fee, str):
                import re
                nums = re.findall(r'[\d,.]+', fee.replace(',', ''))
                fee = int(float(nums[0])) if nums else 50000
            total_ferry_cost += fee
            ferries_passed.append({
                'name': ferry.get('name', 'Phà'),
                'fee': fee,
                'km': ferry_info.get('km_on_route', 0)
            })

            # === Gắn cờ warning nếu user yêu cầu tránh phà ===
            is_violation = bool(user_filters.get('avoid_ferries', False))

            travel_events.append({
                'type': 'ferry',
                'name': ferry.get('name', 'Phà'),
                'cost': fee,
                'km': ferry_info.get('km_on_route', 0),
                'coords': [ferry.get('lat'), ferry.get('lon')] if ferry.get('lat') and ferry.get('lon') else None,
                'details': {'crossing_time_min': ferry.get('crossing_time', 15)},
                'warning': is_violation
            })
    except Exception as e:
        print(f"⚠️ Lỗi khi tính toán ferries (bỏ qua chi tiết): {e}")
        total_ferry_cost = 0
        ferries_on_route = []

    highway_stops_passed = []
    for hw_info in highways_on_route:
        station = hw_info['station']
        highway_stops_passed.append({
            'name': station.get('name', 'Trạm dừng'),
            'km': hw_info.get('km_on_route', 0)
        })

        # === Gắn cờ warning nếu user yêu cầu tránh đường cao tốc hoặc tránh trạm dừng chân ===
        is_violation = bool(user_filters.get('avoid_highways', False) or user_filters.get('avoid_highway_stops', False))

        travel_events.append({
            'type': 'highway_stop',
            'name': station.get('name', 'Trạm dừng cao tốc'),
            'cost': 0,
            'km': hw_info.get('km_on_route', 0),
            'coords': [station['lat'], station['lon']],
            'details': {'amenities': station.get('amenities', [])},
            'warning': is_violation
        })

    travel_events.sort(key=lambda x: x['km'])

    driving_time = (total_route_km / 60) * 60

    print(f"    💰 Phát hiện {len(toll_stations_passed)} trạm thu phí trên đường đi")
    print(f"    🚢 Phát hiện {len(ferries_passed)} phà trên đường đi")

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
        
        # ✅ GEOMETRY: Trả về tọa độ chi tiết
        'geometry_coords': route_coords,  # Mảng [[lat, lon], ...] đầy đủ
        
        'algorithm': 'Direct (No charging needed)'
    }

def calculate_optimal_charge_target(remaining_checkpoints, current_station, current_battery, config):
    """
    TỐI ƯU THÔNG MINH: TÍNH LƯỢNG SẠC TỐI ƯU

    Luôn trả về một số (không bao giờ None). Trả về phần trăm pin mục tiêu (0-100).
    """
    DEFAULT_TARGET = 100

    try:
        # Defensive checks for input
        if not remaining_checkpoints or len(remaining_checkpoints) < 2:
            # Gần đích -> Sạc vừa đủ để đến đích (+ buffer an toàn)
            return min(100, float(current_battery) + 30)

        # Tính khoảng cách đến các checkpoints tiếp theo
        current_pos = remaining_checkpoints[0]
        if not current_pos or (not isinstance(current_pos, (list, tuple)) or len(current_pos) != 2):
            return DEFAULT_TARGET

        max_search_distance = 200  # km

        current_power = current_station.get('power', 0) if current_station else 0

        # Tìm trạm tiếp theo tốt hơn
        next_better_station = find_next_better_station(
            current_pos,
            max_search_distance,
            current_power,
            config
        )

        if next_better_station:
            station_distance = next_better_station.get('distance')
            station_power = next_better_station.get('power', next_better_station.get('station', {}).get('power', 150))

            # Guard against bad distance
            if station_distance is None:
                return DEFAULT_TARGET

            # Tính pin cần để đến trạm đó (+ buffer 10%)
            battery_to_next = calculate_battery_consumption(station_distance, config)
            target = float(current_battery) + battery_to_next + 10

            # CHỈ SẠC VỪA ĐỦ NẾU:
            # 1. Trạm sau công suất cao hơn ít nhất 30kW
            # 2. Cần sạc < 80% (tránh vùng sạc chậm của pin)
            if station_power >= current_power + 30 and target <= 80:
                # print(f"   💡 Tối ưu: Chỉ sạc {target:.0f}% để đến trạm {next_better_station['station'].get('name')} ({station_power}kW)")
                return min(85.0, max(target, float(current_battery) + 20.0))

    except Exception:
        # Silently fallback to DEFAULT
        pass

    return DEFAULT_TARGET

def find_next_better_station(current_pos, max_distance_km, current_power, config):
    """
    Returns: {station, distance, power} hoặc None
    """
    if station_tree is None or not stations:
        return None

    # Validate current_pos
    if not current_pos or (not isinstance(current_pos, (list, tuple)) or len(current_pos) != 2):
        return None

    try:
        k = min(20, len(stations))
        distances, indices = station_tree.query(current_pos, k=k)
    except Exception:
        return None

    better_stations = []

    # Normalize single-result case
    if not isinstance(indices, (list, np.ndarray)):
        indices = [indices]
        distances = [distances]

    for dist_deg, idx in zip(distances, indices):
        try:
            if idx is None:
                continue
            if idx >= len(stations) or idx < 0:
                continue

            station = stations[int(idx)]
            # Compute real geodesic distance (km)
            distance = geodesic(current_pos, (station.get('lat'), station.get('lon'))).km

            station_power = station.get('power') or 0

            if distance <= max_distance_km and station_power >= (current_power or 0) + 30:
                better_stations.append({
                    'station': station,
                    'distance': distance,
                    'power': station_power
                })
        except Exception:
            continue

    if not better_stations:
        return None

    better_stations.sort(key=lambda x: (x['power'] * 1.5) - (x['distance'] * 2), reverse=True)
    return better_stations[0]

def build_candidate_indices(current_node_idx, nodes, config):
    """
    PHIÊN BẢN TỐI ƯU (PRUNED):
    Chỉ trả về các node nằm trong tầm với lý thuyết của xe.
    Giúp giảm độ phức tạp từ O(N^2) xuống O(N*K) với K nhỏ.
    """
    candidates = []
    current_node = nodes[current_node_idx]
    curr_km = current_node['km_position']
    n_nodes = len(nodes)

    # Defensive defaults
    battery_capacity = float(config.get('battery_capacity', 75))
    consumption = float(config.get('consumption', 20))  # kWh / 100km

    # 1. Tính tầm với lý thuyết tối đa (khi đầy bình)
    # max range (km) = (battery_capacity / consumption) * 100
    max_theoretical_range = (battery_capacity / consumption) * 100 * 1.1  # +10% buffer

    # Cửa sổ quay đầu (Backtrack)
    backtrack_window = config.get('backtrack_window_km', 10.0)

    # A. QUÉT PHÍA TRƯỚC (Forward Pruning)
    for i in range(current_node_idx + 1, n_nodes):
        dist = nodes[i]['km_position'] - curr_km
        if dist > max_theoretical_range:
            break  # ⛔ CẮT TỈA: Trạm này quá xa, không bao giờ tới được, dừng vòng lặp ngay
        candidates.append(i)

    # B. QUÉT PHÍA SAU (Backward - Local Backtracking)
    for i in range(current_node_idx - 1, -1, -1):
        dist = curr_km - nodes[i]['km_position']
        if dist > backtrack_window:
            break  # ⛔ CẮT TỈA: Quá xa để quay đầu
        candidates.append(i)

    return candidates

# add_route_to_map removed - map rendering is handled client-side with GoongJS/Leaflet; server returns JSON only.
# Matplotlib comparison chart generation removed - client-side charts are used instead.

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

# create_route_detail_chart removed - charts are generated client-side from JSON data.

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'fav-icon-gg-map.svg', mimetype='image/svg+xml')

@app.route('/api/status-log')
def get_status_log():
    """API để frontend polling trạng thái hiện tại"""
    global CURRENT_PROCESSING_LOG
    return jsonify({"message": CURRENT_PROCESSING_LOG})

@app.route('/')
def index():
    return render_template('index.html', ev_config=EV_CONFIG)

@app.route('/route', methods=['POST'])
def find_route():
    data = request.json
    print(f"🔍 DEBUG DATA RECEIVE: {data}")
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
        log_status(f"Đang định vị địa chỉ: '{start_addr}'...")
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
        
        log_status(f"Đang định vị địa chỉ: '{end_addr}'...")
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
        
        # GET ROUTE STRATEGY: STRICT FILTER FIRST, THEN LOOSE FALLBACK
        # - If user set any filters, try Strict mode (respect filters).
        # - If Strict fails (no routes), try Loose mode (no filters). If Loose succeeds, return
        #   a special 'strict_filter_blocking' response with fallback_data so UI can ask the user.
        route_start = time.time()
        log_status("Đang tìm đường đi tối ưu (Goong API)...")

        candidate_routes_raw = []

        # Detect whether any filters are active (safely parsed)
        user_filters_active = any([
            str2bool(data.get('avoid_tolls', False)),
            str2bool(data.get('avoid_highways', False)),
            str2bool(data.get('avoid_ferries', False)),
            str2bool(data.get('avoid_highway_stops', False))
        ])

        if user_filters_active:
            # 1) Try Strict Mode (respect filters)
            print(f"   🔒 Thử Strict routes (tuân thủ filters)...")
            strict_routes, strict_err = get_route(start_coords, end_coords, data)
            if strict_routes:
                for r in strict_routes:
                    r['source_type'] = 'avoid_strict'
                candidate_routes_raw.extend(strict_routes)
            else:
                # 2) No strict routes found -> try loose (no filters)
                print("   ⚠️ Không tìm thấy lộ trình thỏa bộ lọc nghiêm ngặt — thử chế độ lỏng (bật filters = False)...")
                loose_routes, loose_err = get_route(start_coords, end_coords, options={})
                if loose_routes:
                    # Found routes but blocked by filters: return a structured infeasibility with fallback_data
                    blocked_filters = [k for k in ['avoid_tolls','avoid_highways','avoid_ferries','avoid_highway_stops'] if str2bool(data.get(k, False))]
                    return jsonify({
                        'status': 'warning',
                        'infeasibility': {
                            'error_type': 'strict_filter_blocking',
                            'severity': 'warning',
                            'message': 'Không tìm thấy lộ trình thỏa bộ lọc đã chọn.',
                            'details': 'Hệ thống tìm thấy lộ trình khả thi nếu bỏ một số bộ lọc đã chọn.',
                            'blocked_filters': blocked_filters,
                            'actions': [
                                {
                                    'code': 'retry_ignore_filters',
                                    'label': 'Xem lộ trình có vi phạm',
                                    'params': {'force_ignore': True}
                                }
                            ]
                        },
                        # Optional optimization: include loose route candidates so frontend can show immediately
                        'fallback_data': {
                            'routes': loose_routes
                        }
                    }), 200
                else:
                    # No route even in loose mode — fall through and the normal infeasibility handling will apply
                    print("   ❌ Không tìm thấy lộ trình ngay cả khi bỏ filters.")
        else:
            # If user did not set filters, perform the usual baseline lookup
            print(f"   🏃 Đang tìm Route Baseline (bỏ qua filters)...")
            clean_routes, _ = get_route(start_coords, end_coords, options={})
            if clean_routes:
                for r in clean_routes:
                    r['source_type'] = 'baseline'
                candidate_routes_raw.extend(clean_routes)

        
        print(f"Routing API: {time.time() - route_start:.1f}s")
        
        if not candidate_routes_raw:
            print(f"❌ Không thể lấy route từ Goong/Mapbox")
            return jsonify({
                "error": "Không thể tìm đường đi",
                "error_type": "routing_failed",
                "details": "Không tìm thấy lộ trình nào khả thi giữa hai điểm này.",
                "route_info": {
                    "start": start_addr,
                    "end": end_addr,
                    "direct_distance_km": round(direct_distance, 1)
                },
                "suggestions": [
                    "Thử lại sau vài giây",
                    "Kiểm tra kết nối internet",
                    "Thử nhập địa chỉ khác gần khu vực đó"
                ]
            }), 400
            
        # Deduplicate routes (dựa trên geometry string)
        unique_candidates = {}
        for r in candidate_routes_raw:
            geom = r.get('geometry')
            # Nếu geometry là list, encode nó để làm key
            if isinstance(geom, list):
                try:
                    geom_key = polyline.encode(geom)
                except:
                    geom_key = str(geom)
            else:
                geom_key = geom
                
            if geom_key not in unique_candidates:
                unique_candidates[geom_key] = r
            else:
                # Nếu đã có, ưu tiên giữ lại cái nào là 'avoid_strict' (để biết nó tuân thủ)
                if r.get('source_type') == 'avoid_strict':
                    unique_candidates[geom_key] = r
                    
        final_candidates = list(unique_candidates.values())
        print(f"✅ Có {len(final_candidates)} lộ trình ứng viên để so sánh.")
        
        # TÍNH TOÁN TRẠM SẠC CHO TỪNG ỨNG VIÊN
        calc_start = time.time()
        routes_data = []
        last_infeasibility_error = None
        
        # Build user filters object for A* (để tính warning/penalty)
        user_filters_dict = {
            'avoid_tolls': str2bool(data.get('avoid_tolls', False)),
            'avoid_ferries': str2bool(data.get('avoid_ferries', False)),
            'avoid_highways': str2bool(data.get('avoid_highways', False)),
            'avoid_highway_stops': str2bool(data.get('avoid_highway_stops', False))
        }
        
        for idx, base_route in enumerate(final_candidates):
            try:
                user_config = EV_CONFIG.copy()
                if data.get('battery_capacity'): user_config['battery_capacity'] = float(data['battery_capacity'])
                if data.get('consumption'): user_config['consumption'] = float(data['consumption'])
                if data.get('max_range'): user_config['max_range'] = float(data['max_range'])
                if data.get('charge_speed_fast'): user_config['charge_speed_fast'] = float(data['charge_speed_fast'])
                if data.get('min_battery'): user_config['min_battery'] = float(data['min_battery'])
                
                # Decode geometry
                if base_route.get('steps'):
                    route_coords = decode_full_route_geometry(base_route['steps'])
                else:
                    route_coords = polyline.decode(base_route['geometry'])
                
                # === PHÁT HIỆN CAO TỐC THEO DỮ LIỆU HIGHWAY NODES (ev_database.highways.json) ===
                detected_highways = set()
                used_alternative_for_highways = False
                used_soft_fallback_for_highways = False
                highway_info = []
                highway_warnings = []
                if 'highway_tree' in globals() and highway_tree is not None and route_coords:
                    step = max(1, len(route_coords) // 500)
                    sampled = route_coords[::step]
                    for coord in sampled:
                        try:
                            dist, idx_hw = highway_tree.query(coord, k=1, distance_upper_bound=0.0002)
                            if dist != float('inf') and idx_hw < len(highway_nodes):
                                hw_info = highway_nodes[idx_hw]
                                hw_name = hw_info.get('name') or "Không có tên"
                                if hw_name != "Không có tên":
                                    detected_highways.add(hw_name)
                        except Exception:
                            continue
                    # Keep 'highway_info' for context (always), but only expose 'warnings' if user explicitly requested to avoid highways
                    highway_info = sorted(detected_highways)
                    if detected_highways:
                        log_status(f"⚠️ Phát hiện lộ trình đi qua cao tốc: {', '.join(highway_info)}")

                        # Nếu user yêu cầu né cao tốc, thử Mapbox exclude=motorway để lấy thay thế
                        if user_filters_dict.get('avoid_highways'):
                            highway_warnings = highway_info
                            log_status("🔄 Thử tìm lộ trình tránh cao tốc bằng Mapbox...")
                            alt_routes = get_route_from_mapbox(start_coords, end_coords, user_filters={'avoid_highways': True})
                            if alt_routes:
                                alt_geom = alt_routes[0].get('geometry')
                                alt_coords = []
                                if isinstance(alt_geom, str) and alt_geom:
                                    try:
                                        alt_coords = polyline.decode(alt_geom)
                                    except Exception:
                                        try:
                                            alt_coords = polyline.decode(alt_geom, precision=6)
                                        except Exception:
                                            alt_coords = []
                                elif isinstance(alt_geom, list):
                                    alt_coords = alt_geom

                                # Kiểm tra lại trên lộ trình thay thế
                                alt_detected = set()
                                if alt_coords and 'highway_tree' in globals() and highway_tree is not None:
                                    step2 = max(1, len(alt_coords) // 500)
                                    for coord2 in alt_coords[::step2]:
                                        try:
                                            dist2, idx2 = highway_tree.query(coord2, k=1, distance_upper_bound=0.0002)
                                            if dist2 != float('inf') and idx2 < len(highway_nodes):
                                                name2 = highway_nodes[idx2].get('name') or "Không có tên"
                                                if name2 != "Không có tên":
                                                    alt_detected.add(name2)
                                        except Exception:
                                            continue

                                if not alt_detected:
                                    log_status("✅ Đã tìm được lộ trình thay thế tránh cao tốc bằng Mapbox.")
                                    base_route = alt_routes[0]
                                    if base_route.get('steps'):
                                        route_coords = decode_full_route_geometry(base_route['steps'])
                                    else:
                                        route_coords = polyline.decode(base_route['geometry'])
                                    used_alternative_for_highways = True
                                else:
                                    # Nếu Mapbox trả route nhưng vẫn có cao tốc, báo cảnh báo
                                    log_status(f"⚠️ Không thể né hoàn toàn cao tốc: {', '.join(sorted(alt_detected))}")
                            else:
                                # Soft fallback: nếu Mapbox không trả lộ trình với exclude, thử lại không exclude
                                log_status("⚠️ Mapbox không trả lộ trình thay thế tránh cao tốc — thử soft fallback (không exclude)...")
                                soft_retry = get_route_from_mapbox(start_coords, end_coords, user_filters=None)
                                if soft_retry:
                                    log_status("⚠️ Soft fallback: sử dụng lộ trình không exclude, đánh dấu vi phạm avoid_highways")
                                    base_route = soft_retry[0]
                                    if base_route.get('steps'):
                                        route_coords = decode_full_route_geometry(base_route['steps'])
                                    else:
                                        route_coords = polyline.decode(base_route.get('geometry', '') or '')
                                    used_soft_fallback_for_highways = True
                                else:
                                    log_status("⚠️ Mapbox soft fallback cũng không trả lộ trình")

                result, err = a_star_ev_routing(
                    start_coords, 
                    end_coords, 
                    route_coords, 
                    float(data.get('start_battery', 100)), 
                    user_config, 
                    priority='time',
                    user_filters=user_filters_dict
                )
                
                if result:
                    # Gán thêm thông tin gốc để debug
                    # Lấy source_type từ object base_route, nếu không có thì để mặc định
                    result['source_type'] = base_route.get('source_type', 'unknown')
                    
                    raw_steps = base_route.get('steps', [])
                    turn_by_turn = format_turn_by_turn(raw_steps)
                    road_segments = summarize_route_segments(turn_by_turn)
                    
                    # === LOGIC MỚI: PHÂN TÍCH HIỆU QUẢ BỘ LỌC ===
                    filter_feedback = []
                    
                    # Tính toán vi phạm
                    violation_count = 0
                    if user_filters_dict['avoid_tolls'] and result['num_tolls'] > 0:
                        violation_count += result['num_tolls']
                        filter_feedback.append({'type': 'warning', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">warning</span>', 'text': f"Vẫn phải qua {result['num_tolls']} trạm thu phí"})
                    elif user_filters_dict['avoid_tolls']:
                        filter_feedback.append({'type': 'success', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">check_circle</span>', 'text': 'Đã tránh trạm thu phí'})
                        
                    if user_filters_dict['avoid_ferries'] and result['num_ferries'] > 0:
                        violation_count += result['num_ferries']
                        filter_feedback.append({'type': 'warning', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">warning</span>', 'text': f"Vẫn phải qua {result['num_ferries']} phà"})
                    elif user_filters_dict['avoid_ferries']:
                        filter_feedback.append({'type': 'success', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">directions_boat</span>', 'text': 'Đã tránh phà'})

                    # Highway stops (rest areas) filter feedback
                    if user_filters_dict.get('avoid_highway_stops') and result.get('num_highway_stops', 0) > 0:
                        violation_count += result.get('num_highway_stops', 0)
                        filter_feedback.append({'type': 'warning', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">warning</span>', 'text': f"Vẫn phải qua {result.get('num_highway_stops', 0)} trạm dừng chân"})
                    elif user_filters_dict.get('avoid_highway_stops'):
                        filter_feedback.append({'type': 'success', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">check_circle</span>', 'text': 'Đã tránh trạm dừng chân'})

                    # Motorway/highway detection feedback (using highway_nodes)
                    if highway_warnings:
                        if user_filters_dict.get('avoid_highways'):
                            if used_alternative_for_highways:
                                filter_feedback.append({'type': 'success', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">check_circle</span>', 'text': 'Đã tránh cao tốc'})
                            elif used_soft_fallback_for_highways:
                                filter_feedback.append({'type': 'warning', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">warning</span>', 'text': f"Không thể tránh hoàn toàn cao tốc — sử dụng lộ trình có cao tốc: {', '.join(highway_warnings)}"})
                    elif user_filters_dict.get('avoid_highways'):
                        filter_feedback.append({'type': 'success', 'icon': '<span class="icon material-symbols-rounded" style="font-size:14px">check_circle</span>', 'text': 'Đã tránh cao tốc'})

                    result['violation_count'] = violation_count

                    routes_data.append({
                        'name': f'Route {idx + 1}',
                        'route': base_route,
                        'coords': route_coords,
                        'stops': result['charging_stops'],
                        'metrics': result,
                        'feasible': True,
                        'color': '#94a3b8',
                        'is_best': False,
                        'instructions': turn_by_turn,
                        'segments': road_segments,
                        'filter_feedback': filter_feedback,
                        # Provide full highway info (context), but only expose warnings if user requested avoid_highways
                        'highway_info': highway_info,
                        'highway_warnings': highway_warnings if user_filters_dict.get('avoid_highways') else [],
                        'used_alternative_for_highways': used_alternative_for_highways,
                        'used_soft_fallback_for_highways': used_soft_fallback_for_highways
                    })
                else:
                    if isinstance(err, dict) and err.get('error') == 'infeasible':
                        last_infeasibility_error = err
            except Exception as e:
                print(f"❌ Lỗi xử lý route {idx}: {e}")
                traceback.print_exc()
                continue
        
        print(f"⏱️ Tính trạm sạc: {time.time() - calc_start:.1f}s")
        
        if not routes_data:
            print(f"❌ KHÔNG CÓ LỘ TRÌNH KHẢ THI")
            current_battery = float(data.get('start_battery', 100))
            km_can_travel = calculate_km_from_battery(current_battery, EV_CONFIG)
            
            if last_infeasibility_error:
                last_infeasibility_error['map_html'] = None 
                return jsonify({"infeasibility": last_infeasibility_error}), 200
            
            default_infeasibility = {
                "error": "infeasible",
                "error_type": "no_feasible_route",
                "severity": "critical",
                "location": {"km_from_start": 0, "km_to_destination": round(direct_distance, 1)},
                "battery_state": {
                    "current_percent": round(current_battery, 1),
                    "max_range_km": round(km_can_travel, 1),
                    "needed_km": round(direct_distance, 1),
                    "deficit_percent": round(max(0, (direct_distance - km_can_travel) / km_can_travel * 100), 1) if km_can_travel > 0 else 100
                },
                "suggestions": ["Sạc pin lên 100%", "Chọn xe pin lớn hơn"],
                "message": f"🔋 Không có lộ trình khả thi!",
                "details": f"Không tìm thấy trạm sạc phù hợp.",
                "map_html": None
            }
            return jsonify({"infeasibility": default_infeasibility}), 200
        
        # === DYNAMIC SORTING THEO PRIORITY (SOFT CONSTRAINTS) ===
        priority = data.get('priority', 'time')
        print(f"📊 Sắp xếp theo tiêu chí: {priority}")
        
        # Định nghĩa Penalty cho Soft Constraints (để ưu tiên route tuân thủ nếu chênh lệch không đáng kể)
        PENALTY_COST = 20000  # 20k VND
        PENALTY_DIST = 5.0    # 5 km
        PENALTY_TIME = 10.0   # 10 phút
        
        def get_sort_key(item):
            m = item['metrics']
            # Violation score: number of filter violations (0 is best)
            violation_score = m.get('violation_count', 0)

            # Primary value depends on chosen priority
            if priority == 'cost':
                primary_value = m.get('total_cost', 0)
            elif priority == 'distance':
                primary_value = m.get('total_distance', 0)
            elif priority == 'charges':
                # For charges prefer fewer charges, then lower time
                primary_value = (m.get('num_charges', 0), m.get('total_time', 0))
            else:  # 'time' or default
                primary_value = m.get('total_time', 0)

            # Return a tuple so Python sorts by violation first, then by priority
            if isinstance(primary_value, tuple):
                return (violation_score,) + primary_value
            return (violation_score, primary_value)

        routes_data.sort(key=get_sort_key)
        
        # === LOGIC MỚI: GÁN TÊN VÀ MÀU SẮC SAU KHI ĐÃ BIẾT AI LÀ NHẤT ===
        for i, route in enumerate(routes_data):
            if i == 0:
                route['name'] = 'Lộ trình tối ưu'
                route['color'] = '#3b82f6'
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
        
        avoid_ferries = str2bool(data.get('avoid_ferries', False))
        avoid_highways = str2bool(data.get('avoid_highways', False))
        avoid_tolls = str2bool(data.get('avoid_tolls', False))
        
        nearby_ferries = find_ferries_near_route(best_route_coords, max_distance_km=5) if avoid_ferries else []
        # Always locate nearby highway stations (we need their coords to show warnings/highlight mandatory segments)
        nearby_highway_stations = find_highway_stations_near_route(best_route_coords, max_distance_km=3)
        nearby_toll_stations = find_toll_stations_near_route(best_route_coords, max_distance_km=2) if avoid_tolls else []
        
        print(f"⏱️ Tìm trạm gần đường: {time.time() - nearby_start:.1f}s")
        
        # CHUẨN BỊ DỮ LIỆU JSON (Client-Side Rendering)
        json_start = time.time()
        
        # Chuẩn bị dữ liệu Nearby Features
        nearby_data = {
            "charging": [{'lat': s['station']['lat'], 'lon': s['station']['lon'], 'name': s['station']['name'], 'type': s['station'].get('type'), 'power': s['station'].get('power')} for s in nearby_charging_stations],
            "ferries": [{'lat': f['ferry']['lat'], 'lon': f['ferry']['lon'], 'name': f['ferry']['name']} for f in nearby_ferries] if nearby_ferries else [],
            "tolls": [{'lat': t['station']['lat'], 'lon': t['station']['lon'], 'name': t['station']['name']} for t in nearby_toll_stations] if nearby_toll_stations else [],
            "highways": [{'lat': h['station']['lat'], 'lon': h['station']['lon'], 'name': h['station']['name'], 'highway': h['station'].get('highway'), 'address': h['station'].get('address'), 'amenities': h['station'].get('amenities')} for h in nearby_highway_stations] if nearby_highway_stations else []
        }
        
        print(f"⏱️ Chuẩn bị dữ liệu: {time.time() - json_start:.1f}s")
        print(f"\n✅ TỔNG THỜI GIAN: {time.time() - total_start:.1f}s")
        print(f"{'='*60}\n")
        
        return jsonify({
            "status": "success",
            "routes": [{
                'name': r['name'], 
                'metrics': r['metrics'], 
                'charging_stops': r['stops'],
                # Charts are generated client-side now; backend returns None to avoid invoking removed server-side functions.
                'detail_chart': None,
                'is_best': r.get('is_best', False),
                'instructions': r['instructions'],
                'segments': r.get('segments', []),
                'travel_events': r['metrics'].get('travel_events', []),
                'color': r['color'],
                'filter_feedback': r.get('filter_feedback', []),
                'highway_warnings': r.get('highway_warnings', []),
                'used_alternative_for_highways': r.get('used_alternative_for_highways', False),

                # QUAN TRỌNG: Gửi tọa độ thô xuống để JS vẽ
                'geometry_coords': r['coords'] 
            } for r in routes_data],
            
            "nearby_features": nearby_data
        })
        
    except Exception as e:
        print(f"\n❌ LỖI NGHIÊM TRỌNG: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Lỗi hệ thống: {str(e)}"}), 500


@app.route('/route-debug', methods=['POST'])
def route_debug():
    """Debug endpoint: returns geometry statistics and highway stop counts for candidate routes.
    Useful to reproduce quality issues (e.g., Hanoi -> Da Nang) without running full A*.
    """
    data = request.json
    start_addr = data.get('start')
    end_addr = data.get('end')
    # Allow passing coords directly: start_coords: [lat, lon], end_coords: [lat, lon]
    start_coords_in = data.get('start_coords')
    end_coords_in = data.get('end_coords')
    try:
        # Resolve coordinates
        if start_coords_in and end_coords_in:
            start_coords = (float(start_coords_in[0]), float(start_coords_in[1]))
            end_coords = (float(end_coords_in[0]), float(end_coords_in[1]))
        else:
            # Basic geocode to coords
            start_loc = geolocator.geocode(start_addr + ", Việt Nam", timeout=15)
            end_loc = geolocator.geocode(end_addr + ", Việt Nam", timeout=15)
            if not start_loc or not end_loc:
                return jsonify({"error": "Geocoding failed"}), 400

            start_coords = (start_loc.latitude, start_loc.longitude)
            end_coords = (end_loc.latitude, end_loc.longitude)

        # Fetch routes (Goong-first)
        routes, _ = get_route(start_coords, end_coords, options={})
        debug = []
        for r in routes:
            geom = r.get('geometry')
            coords = []
            if isinstance(geom, str) and geom:
                try:
                    coords = polyline.decode(geom)
                except Exception:
                    try:
                        coords = polyline.decode(geom, precision=6)
                    except Exception:
                        coords = []
            elif isinstance(geom, list) and geom:
                coords = geom

            # compute stats
            point_count = len(coords)
            max_gap = 0.0
            if point_count > 1:
                for i in range(point_count - 1):
                    try:
                        d = geodesic((coords[i][0], coords[i][1]), (coords[i+1][0], coords[i+1][1])).km
                        if d > max_gap: max_gap = d
                    except Exception:
                        continue

            highways = find_highway_stations_on_route(coords, max_distance_km=0.1) if coords else []
            debug.append({
                'name': r.get('name'),
                'point_count': point_count,
                'max_gap_km': round(max_gap, 3),
                'num_highway_stops': len(highways),
                'has_steps': bool(r.get('steps'))
            })

        return jsonify({'debug': debug}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

def find_highway_stations_near_route(route_coords, max_distance_km=3):
    """Tìm trạm cao tốc - TỐI ƯU

    Sử dụng `highway_nodes` + `highway_tree` (nếu có) để truy vấn nhanh,
    fallback sang `highway_stations` nếu dữ liệu Mongo có sẵn hoặc JSON không tồn tại.
    """
    nearby_stations = []

    # Prefer KD-Tree on highway_nodes for speed
    if 'highway_tree' in globals() and highway_tree is not None and len(highway_nodes) > 0:
        # Sample route to reduce queries
        num_samples = min(50, len(route_coords))
        step = max(1, len(route_coords) // num_samples)
        sampled_coords = route_coords[::step]

        for coord in sampled_coords:
            try:
                # distance_upper_bound in degrees approx (0.01 ~ 1.11 km) so convert km to degrees roughly
                deg_bound = max(0.0001, max_distance_km / 111.0)
                d, idx = highway_tree.query((coord[0], coord[1]), k=5, distance_upper_bound=deg_bound)
                # idx or d may be scalars depending on k
                if np.isscalar(idx):
                    idxs = [idx]
                    ds = [d]
                else:
                    idxs = idx
                    ds = d
                for dist_deg, i in zip(ds, idxs):
                    if dist_deg == float('inf'):
                        continue
                    if i is None:
                        continue
                    if int(i) >= len(highway_nodes):
                        continue
                    # compute precise geodesic distance
                    hw = highway_nodes[int(i)]
                    try:
                        precise_km = geodesic((hw['lat'], hw['lon']), (coord[0], coord[1])).km
                    except Exception:
                        precise_km = None
                    if precise_km is not None and precise_km <= max_distance_km:
                        nearby_stations.append({'station': hw, 'distance_to_route': round(precise_km, 3)})
            except Exception:
                continue
    else:
        # Fallback: linear scan through highway_stations loaded from MongoDB
        if len(highway_stations) == 0:
            return []
        num_samples = min(10, len(route_coords))
        step = max(1, len(route_coords) // num_samples)
        sampled_coords = route_coords[::step]
        for station in highway_stations:
            station_pos = (station['lat'], station['lon'])
            try:
                min_distance = min(geodesic(station_pos, coord).km for coord in sampled_coords)
            except Exception:
                continue
            if min_distance <= max_distance_km:
                nearby_stations.append({
                    'station': station,
                    'distance_to_route': round(min_distance, 2)
                })

    # Deduplicate by station id/name and sort
    unique = {}
    for s in nearby_stations:
        key = s['station'].get('id') or s['station'].get('name') or json.dumps(s['station'])
        if key not in unique or unique[key]['distance_to_route'] > s['distance_to_route']:
            unique[key] = s

    result = sorted(unique.values(), key=lambda x: x['distance_to_route'])
    return result[:3]

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