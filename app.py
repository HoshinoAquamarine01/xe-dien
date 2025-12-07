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
collection = db['ev_stations']
stations = list(collection.find({}, {'_id': 0}))
client.close()

station_coords = np.array([[s['lat'], s['lon']] for s in stations])
station_tree = cKDTree(station_coords)
print(f"Đã tải {len(stations)} trạm sạc. Hệ thống sẵn sàng!")
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

def add_stations_to_map(map_obj):
    """
    TỐI ƯU HIỂN THỊ BẢN ĐỒ:
    - Sử dụng MarkerCluster để gom nhóm khi ở xa (tránh lag).
    - Tự động bung ra (disableClustering) khi zoom vào mức 14 (cấp phường/xã).
    """
    marker_cluster = MarkerCluster(
        name="Trạm sạc VinFast",
        options={
            'disableClusteringAtZoom': 14,  # <--- QUAN TRỌNG: Zoom đến 14 là hiện icon
            'spiderfyOnMaxZoom': True
        }
    ).add_to(map_obj)

    for station in stations:
        # Phân loại màu sắc icon
        icon_color = "green" if station.get('type') == 'AC' else "red"
        
        # Popup thông tin chi tiết
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

def get_route(start_coords, end_coords, options=None):
    """Lấy route từ OSRM API"""
    url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"
    params = {
        "overview": "full",
        "geometries": "polyline",
        "steps": "false",
        "alternatives": "false"
    }
    
    if options:
        excludes = []
        if options.get('avoid_highways'): excludes.append('motorway')
        if options.get('avoid_tolls'): excludes.append('toll')
        if options.get('avoid_ferries'): excludes.append('ferry')
        if excludes: params['exclude'] = ','.join(excludes)
    
    try:
        response = requests.get(url, params=params, timeout=3)
        if response.status_code != 200: return []
        data = response.json()
        
        routes = []
        if data["code"] == "Ok":
            for route in data["routes"]:
                routes.append({
                    "geometry": route["geometry"],
                    "distance": route["distance"],
                    "duration": route["duration"],
                    "steps": route.get("legs", [{}])[0].get("steps", [])
                })
        return routes
    except Exception as e:
        print(f"Lỗi OSRM: {e}")
        return []

def calculate_battery_consumption(distance_km, config):
    kwh_used = (distance_km / 100) * config['consumption']
    return (kwh_used / config['battery_capacity']) * 100

def calculate_km_from_battery(battery_percent, config):
    kwh_available = (battery_percent / 100) * config['battery_capacity']
    return (kwh_available / config['consumption']) * 100

def find_nearest_reachable_station(current_pos, battery_percent, config):
    km_can_travel = calculate_km_from_battery(battery_percent, config)
    
    # Tìm 10 trạm gần nhất bằng KD-Tree
    distances, indices = station_tree.query(current_pos, k=min(10, len(stations)))
    
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
            
            # Sạc đến 90% hoặc đủ để đi tiếp + dự phòng
            distance_to_next = geodesic((station['lat'], station['lon']), checkpoints[i+1]).km
            battery_needed_after_station = calculate_battery_consumption(distance_to_next, config)
            target_battery = min(90, battery_on_arrival + battery_needed_after_station + 20)  # +20% dự phòng
            
            charge_time = calculate_charge_time(battery_on_arrival, target_battery, station.get('power', 150), config)
            
            charging_stops.append({
                'station': station,
                'arrival_battery': round(battery_on_arrival, 1),
                'start_charge': round(battery_on_arrival, 1),
                'end_charge': round(target_battery, 1),
                'charge_time': round(charge_time, 1),
                'distance_from_start': round(total_distance + reachable[0]['distance'], 1)
            })
            
            # CẬP NHẬT PIN SAU KHI SẠC - QUAN TRỌNG!
            battery_percent = target_battery
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
    
    driving_time = (route_distance / 60) * 60  # Giả sử trung bình 60km/h
    charging_time = sum(s['charge_time'] for s in charging_stops)
    
    return {
        'total_distance': round(route_distance, 2),
        'driving_time': round(driving_time, 1),
        'charging_time': round(charging_time, 1),
        'total_time': round(driving_time + charging_time, 1),
        'num_charges': len(charging_stops),
        'charging_stops': charging_stops,
        'final_battery': round(max(0, battery_percent), 1),  # Đảm bảo không âm
        'feasible': True
    }, None

def add_route_to_map(map_obj, route_coords, charging_stops, color="blue", label=""):
    """Thêm route và highlight các trạm sạc đã chọn"""
    # Vẽ đường đi
    step = max(1, len(route_coords) // 100)
    folium.PolyLine(route_coords[::step], color=color, weight=6, opacity=0.8, 
                    popup=label).add_to(map_obj)
    
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
    plt.rcParams['font.size'] = 9
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), dpi=80)
    fig.patch.set_facecolor('white')
    fig.patch.set_alpha(0.95)
    
    names = [r['name'] for r in routes_data]
    colors = [r['color'] for r in routes_data]
    
    # 1. Quãng đường
    dists = [r['metrics']['total_distance'] for r in routes_data]
    axes[0,0].bar(names, dists, color=colors, alpha=0.8)
    axes[0,0].set_title('Quãng đường (km)', fontweight='bold', pad=10)
    axes[0,0].set_ylabel('km')
    axes[0,0].grid(axis='y', alpha=0.3, linestyle='--')
    
    # 2. Thời gian
    d_time = [r['metrics']['driving_time'] for r in routes_data]
    c_time = [r['metrics']['charging_time'] for r in routes_data]
    x = np.arange(len(names))
    axes[0,1].bar(x, d_time, width=0.5, label='Lái xe', alpha=0.8)
    axes[0,1].bar(x, c_time, width=0.5, bottom=d_time, label='Sạc pin', alpha=0.8)
    axes[0,1].set_xticks(x)
    axes[0,1].set_xticklabels(names, rotation=0)
    axes[0,1].legend(loc='upper right')
    axes[0,1].set_title('Thời gian (phút)', fontweight='bold', pad=10)
    axes[0,1].set_ylabel('phút')
    axes[0,1].grid(axis='y', alpha=0.3, linestyle='--')

    # 3. Số lần sạc
    charges = [r['metrics']['num_charges'] for r in routes_data]
    axes[1,0].bar(names, charges, color=colors, alpha=0.8)
    axes[1,0].set_title('Số lần sạc', fontweight='bold', pad=10)
    axes[1,0].set_ylabel('lần')
    axes[1,0].grid(axis='y', alpha=0.3, linestyle='--')

    # 4. Tổng thời gian
    total = [r['metrics']['total_time'] for r in routes_data]
    axes[1,1].bar(names, total, color=colors, alpha=0.8)
    axes[1,1].set_title('Tổng thời gian (phút)', fontweight='bold', pad=10)
    axes[1,1].set_ylabel('phút')
    axes[1,1].grid(axis='y', alpha=0.3, linestyle='--')
    
    plt.tight_layout(pad=2.0)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=80, facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"

def create_route_detail_chart(route_data):
    if not route_data['stops']: return None
    plt.rcParams['font.family'] = 'DejaVu Sans'
    plt.rcParams['font.size'] = 9
    fig, ax = plt.subplots(figsize=(10, 4), dpi=80)
    fig.patch.set_facecolor('white')
    fig.patch.set_alpha(0.95)
    
    stops = route_data['stops']
    names = [f"Trạm {i+1}" for i in range(len(stops))]
    arrival = [s['arrival_battery'] for s in stops]
    departure = [s['end_charge'] for s in stops]
    
    x = np.arange(len(names))
    width = 0.35
    
    bars1 = ax.bar(x - width/2, arrival, width, label='Pin khi đến', color='#ef4444', alpha=0.8)
    bars2 = ax.bar(x + width/2, departure, width, label='Pin sau sạc', color='#10b981', alpha=0.8)
    
    ax.set_xlabel('Trạm sạc', fontweight='bold')
    ax.set_ylabel('Mức pin (%)', fontweight='bold')
    ax.set_title('Biểu đồ Pin tại các trạm sạc', fontweight='bold', pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels(names)
    ax.legend(loc='upper right')
    ax.grid(axis='y', alpha=0.3, linestyle='--')
    ax.set_ylim([0, 100])
    
    # Thêm giá trị trên cột
    for bars in [bars1, bars2]:
        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   f'{height:.0f}%', ha='center', va='bottom', fontsize=8)
    
    plt.tight_layout(pad=1.5)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=80, facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('public', 'avt.ico', mimetype='image/x-icon')

@app.route('/')
def index():
    return render_template('index.html', ev_config=EV_CONFIG)

@app.route('/route', methods=['POST'])
def find_route():
    data = request.json
    start_addr, end_addr = data.get('start'), data.get('end')
    
    try:
        start_loc = geolocator.geocode(start_addr + ", Việt Nam")
        end_loc = geolocator.geocode(end_addr + ", Việt Nam")
        if not start_loc or not end_loc: return jsonify({"error": "Không tìm thấy địa chỉ"}), 400
        
        start_coords = (start_loc.latitude, start_loc.longitude)
        end_coords = (end_loc.latitude, end_loc.longitude)
        
        base_routes = get_route(start_coords, end_coords, data)
        if not base_routes: return jsonify({"error": "Không tìm thấy đường đi"}), 400
        
        routes_data = []
        base_route = base_routes[0]
        route_coords = polyline.decode(base_route['geometry'])
        result, err = a_star_ev_routing(start_coords, end_coords, route_coords, float(data.get('start_battery', 100)), EV_CONFIG)
        
        if result:
            routes_data.append({
                'name': 'Lộ trình tối ưu', 'route': base_route, 'coords': route_coords,
                'stops': result['charging_stops'], 'metrics': result, 'feasible': True,
                'color': '#3b82f6'
            })
        
        if not routes_data: return jsonify({"error": "Không có lộ trình khả thi cho xe điện"}), 400
        
        # Tạo bản đồ với trung tâm ở giữa start và end
        center_lat = (start_coords[0] + end_coords[0]) / 2
        center_lon = (start_coords[1] + end_coords[1]) / 2
        m = folium.Map(
            location=[center_lat, center_lon], 
            zoom_start=8,
            tiles='OpenStreetMap',
            prefer_canvas=True
        )
        
        # THÊM TẤT CẢ TRẠM SẠC VỚI CLUSTER
        add_stations_to_map(m)
        
        # Thêm lộ trình và highlight trạm sạc đã chọn
        for r in routes_data:
            add_route_to_map(m, r['coords'], r['stops'], r['color'], r['name'])
        
        # Thêm legend đẹp hơn
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
                    min-width: 220px;
                    border: 2px solid #e2e8f0;">
            <h4 style="margin: 0 0 15px 0; 
                       font-size: 16px; 
                       color: #1e293b;
                       border-bottom: 2px solid #6366f1;
                       padding-bottom: 10px;">
                📋 Chú thích
            </h4>
            <div style="line-height: 2;">
                <div><i class="fa fa-play" style="color: green; width: 20px;"></i> Điểm xuất phát</div>
                <div><i class="fa fa-flag-checkered" style="color: red; width: 20px;"></i> Điểm đến</div>
                <div><i class="fa fa-bolt" style="color: orange; width: 20px;"></i> <b>Trạm sạc đã chọn</b></div>
                <div><i class="fa fa-bolt" style="color: red; width: 20px;"></i> Trạm DC Fast</div>
                <div><i class="fa fa-bolt" style="color: green; width: 20px;"></i> Trạm AC</div>
            </div>
            <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #64748b;">
                Zoom vào để xem chi tiết trạm
            </div>
        </div>
        '''
        m.get_root().html.add_child(folium.Element(legend_html))
        
        # Auto fit bounds với padding
        all_coords = routes_data[0]['coords']
        if all_coords:
            m.fit_bounds(all_coords, padding=[50, 50])
            
        return jsonify({
            "map": m._repr_html_(),
            "routes": [{
                'name': r['name'], 'metrics': r['metrics'], 'charging_stops': r['stops'],
                'detail_chart': create_route_detail_chart(r)
            } for r in routes_data],
            "comparison_chart": create_comparison_chart(routes_data)
        })

    except Exception as e:
        print(e)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)