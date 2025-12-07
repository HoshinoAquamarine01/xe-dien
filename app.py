import folium
import json
import polyline
import requests
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from flask import Flask, render_template, request, jsonify
import heapq
import math
import matplotlib
matplotlib.use('Agg')  # Backend cho server
import matplotlib.pyplot as plt
import io
import base64
from matplotlib import font_manager
import numpy as np

app = Flask(__name__)

# Load trạm sạc
with open('ev_stations.json', 'r', encoding='utf-8') as f:
    stations = json.load(f)

# Geocoder
geolocator = Nominatim(user_agent="ev-app-hcm")

# Cấu hình EV mặc định
EV_CONFIG = {
    "battery_capacity": 75,  # kWh
    "consumption": 16.3,     # kWh/100km (theo đề bài)
    "max_range": 460,        # km (75 kWh / 16.3 * 100)
    "charge_speed_fast": 150, # kW (DC Fast)
    "charge_speed_slow": 50,  # kW (AC)
    "min_battery": 20,       # % ngưỡng an toàn
    "charge_target": 80      # % mục tiêu sạc
}

class EVState:
    """Trạng thái của xe EV trong thuật toán tìm kiếm"""
    def __init__(self, position, battery_percent, km_remaining, distance_traveled, 
                 time_spent, charging_stops, path_coords):
        self.position = position  # (lat, lon)
        self.battery_percent = battery_percent  # %
        self.km_remaining = km_remaining  # km còn đi được
        self.distance_traveled = distance_traveled  # Tổng km đã đi
        self.time_spent = time_spent  # Tổng thời gian (phút)
        self.charging_stops = charging_stops  # List các lần sạc
        self.path_coords = path_coords  # Danh sách tọa độ đã đi
        
    def __lt__(self, other):
        """So sánh cho priority queue"""
        return self.time_spent < other.time_spent

def create_map(center=[10.76, 106.66], zoom=12):
    """Tạo bản đồ cơ bản"""
    return folium.Map(location=center, zoom_start=zoom)

def add_stations_to_map(map_obj):
    """Thêm tất cả trạm sạc lên bản đồ"""
    for station in stations:
        power_type = "DC Fast" if station.get('power', 0) >= 100 else "AC"
        folium.Marker(
            [station["lat"], station["lon"]],
            popup=f"""<b>{station['name']}</b><br>
                     {station['address']}<br>
                     Loại: {station.get('type', 'AC/DC')}<br>
                     Công suất: {station.get('power', 50)} kW""",
            tooltip=f"{station['name']} ({station.get('power', 50)}kW)",
            icon=folium.Icon(color="green", icon="bolt", prefix='fa')
        ).add_to(map_obj)

def get_route(start_coords, end_coords, options=None):
    """Lấy route từ OSRM API với các tùy chọn"""
    url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"
    params = {
        "overview": "full",
        "geometries": "polyline",
        "steps": "true",
        "alternatives": "true"  # Lấy nhiều lộ trình
    }
    
    # Thêm các tùy chọn tránh đường
    if options:
        excludes = []
        if options.get('avoid_highways'):
            excludes.append('motorway')
        if options.get('avoid_tolls'):
            excludes.append('toll')
        if options.get('avoid_ferries'):
            excludes.append('ferry')
        
        if excludes:
            params['exclude'] = ','.join(excludes)
    
    response = requests.get(url, params=params)
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

def calculate_battery_consumption(distance_km, config):
    """Tính pin tiêu thụ theo km"""
    kwh_used = (distance_km / 100) * config['consumption']
    percent_used = (kwh_used / config['battery_capacity']) * 100
    return percent_used

def calculate_km_from_battery(battery_percent, config):
    """Tính số km có thể đi với % pin hiện tại"""
    kwh_available = (battery_percent / 100) * config['battery_capacity']
    km_available = (kwh_available / config['consumption']) * 100
    return km_available

def find_nearest_reachable_station(current_pos, battery_percent, config):
    """Tìm trạm sạc gần nhất có thể đến được"""
    km_can_travel = calculate_km_from_battery(battery_percent, config)
    
    reachable_stations = []
    for station in stations:
        station_pos = (station['lat'], station['lon'])
        distance = geodesic(current_pos, station_pos).km
        
        # Kiểm tra có đủ pin đến trạm không (với buffer 5%)
        battery_needed = calculate_battery_consumption(distance, config)
        if battery_percent - battery_needed >= 5:  # Buffer 5%
            reachable_stations.append({
                'station': station,
                'distance': distance,
                'battery_on_arrival': battery_percent - battery_needed
            })
    
    # Sắp xếp theo khoảng cách
    reachable_stations.sort(key=lambda x: x['distance'])
    return reachable_stations

def calculate_charge_time(current_battery, target_battery, power_kw, config):
    """Tính thời gian sạc (phút) từ current đến target"""
    charge_amount = target_battery - current_battery
    energy_kwh = (charge_amount / 100) * config['battery_capacity']
    
    # Tính thời gian với đường cong sạc (chậm dần khi gần đầy)
    if target_battery <= 80:
        hours = energy_kwh / power_kw
    else:
        # Sạc từ 80-100% chậm hơn ~50%
        hours = (energy_kwh / power_kw) * 1.5
    
    return hours * 60

def heuristic(current_pos, goal_pos):
    """Hàm heuristic cho A* (khoảng cách thẳng)"""
    return geodesic(current_pos, goal_pos).km

def a_star_ev_routing(start_coords, end_coords, route_coords, start_battery, config, priority='time'):
    """
    Thuật toán A* để tìm lộ trình tối ưu với sạc pin
    
    State = (vị_trí_hiện_tại, %_pin_còn_lại, km_còn_đi_được)
    """
    # Priority queue: (f_score, state)
    start_state = EVState(
        position=start_coords,
        battery_percent=start_battery,
        km_remaining=calculate_km_from_battery(start_battery, config),
        distance_traveled=0,
        time_spent=0,
        charging_stops=[],
        path_coords=[start_coords]
    )
    
    # f(n) = g(n) + h(n)
    h_start = heuristic(start_coords, end_coords)
    f_start = 0 + h_start
    
    open_set = [(f_start, 0, start_state)]  # (f_score, counter, state)
    counter = 1
    visited = set()
    
    # Mô phỏng lái xe dọc theo route
    route_distance = sum(geodesic(route_coords[i], route_coords[i+1]).km 
                        for i in range(len(route_coords)-1))
    
    # Chia route thành các segments nhỏ để kiểm tra pin
    segment_length = 10  # km
    current_idx = 0
    current_state = start_state
    
    charging_stops = []
    total_time = 0
    total_distance = 0
    
    while current_idx < len(route_coords) - 1:
        current_pos = route_coords[current_idx]
        
        # Tính khoảng cách đến điểm tiếp theo
        next_pos = route_coords[min(current_idx + 1, len(route_coords) - 1)]
        segment_dist = geodesic(current_pos, next_pos).km
        
        # Kiểm tra pin
        battery_needed = calculate_battery_consumption(segment_dist, config)
        
        if current_state.battery_percent - battery_needed < config['min_battery']:
            # CẦN SẠC - Tìm trạm sạc gần nhất
            reachable = find_nearest_reachable_station(
                current_pos, 
                current_state.battery_percent, 
                config
            )
            
            if not reachable:
                # KHÔNG THỂ TIẾP TỤC - Lộ trình không khả thi
                return None, "Không tìm thấy trạm sạc phù hợp. Lộ trình không khả thi."
            
            # Chọn trạm tốt nhất
            best_station = reachable[0]
            station = best_station['station']
            
            # Tính toán cho việc sạc
            distance_to_station = best_station['distance']
            battery_on_arrival = best_station['battery_on_arrival']
            
            # Tính lượng sạc cần thiết (sạc thông minh)
            remaining_distance = sum(geodesic(route_coords[i], route_coords[i+1]).km 
                                    for i in range(current_idx, len(route_coords)-1))
            
            # Ước tính pin cần cho phần còn lại + buffer
            battery_needed_for_rest = calculate_battery_consumption(remaining_distance, config)
            target_battery = min(battery_on_arrival + battery_needed_for_rest * 1.3, config['charge_target'])
            
            # Nếu có trạm sạc mạnh, có thể sạc thêm
            if station.get('power', 50) >= 100:
                target_battery = min(target_battery + 10, 90)
            
            charge_amount = target_battery - battery_on_arrival
            charge_time = calculate_charge_time(
                battery_on_arrival, 
                target_battery, 
                station.get('power', 50),
                config
            )
            
            # Cập nhật trạng thái sau khi sạc
            charging_stops.append({
                'station': station,
                'arrival_battery': round(battery_on_arrival, 1),
                'start_charge': round(battery_on_arrival, 1),
                'end_charge': round(target_battery, 1),
                'charge_amount': round(charge_amount, 1),
                'charge_time': round(charge_time, 1),
                'distance_from_start': round(total_distance + distance_to_station, 1)
            })
            
            total_time += (distance_to_station / 60) * 60  # Thời gian đi đến trạm
            total_time += charge_time
            total_distance += distance_to_station
            
            current_state.battery_percent = target_battery
            current_state.km_remaining = calculate_km_from_battery(target_battery, config)
        
        # Tiếp tục lái xe
        current_state.battery_percent -= battery_needed
        current_state.km_remaining = calculate_km_from_battery(current_state.battery_percent, config)
        total_distance += segment_dist
        total_time += (segment_dist / 60) * 60  # Giả sử trung bình 60 km/h
        
        current_idx += 1
    
    # Hoàn thành lộ trình
    driving_time = (route_distance / 60) * 60  # phút
    charging_time = sum(stop['charge_time'] for stop in charging_stops)
    
    result = {
        'total_distance': round(route_distance, 2),
        'driving_time': round(driving_time, 1),
        'charging_time': round(charging_time, 1),
        'total_time': round(driving_time + charging_time, 1),
        'num_charges': len(charging_stops),
        'charging_stops': charging_stops,
        'final_battery': round(current_state.battery_percent, 1),
        'feasible': True
    }
    
    return result, None

def calculate_route_metrics(route, charging_stops):
    """Tính toán các chỉ số của lộ trình"""
    total_distance = route['distance'] / 1000  # km
    driving_time = route['duration'] / 60  # phút
    charging_time = sum(stop['charge_time'] for stop in charging_stops)
    total_time = driving_time + charging_time
    
    return {
        'total_distance': round(total_distance, 2),
        'driving_time': round(driving_time, 1),
        'charging_time': round(charging_time, 1),
        'total_time': round(total_time, 1),
        'num_charges': len(charging_stops)
    }

def add_route_to_map(map_obj, route_coords, charging_stops, color="blue", label=""):
    """Vẽ lộ trình và trạm sạc lên bản đồ"""
    # Vẽ route
    folium.PolyLine(
        route_coords, 
        color=color, 
        weight=5, 
        opacity=0.7,
        popup=label
    ).add_to(map_obj)
    
    # Đánh dấu điểm đầu và cuối
    folium.Marker(
        route_coords[0], 
        icon=folium.Icon(color="red", icon="play"),
        popup="Điểm xuất phát"
    ).add_to(map_obj)
    
    folium.Marker(
        route_coords[-1], 
        icon=folium.Icon(color="darkred", icon="stop"),
        popup="Điểm đến"
    ).add_to(map_obj)
    
    # Đánh dấu các trạm sạc
    for i, stop in enumerate(charging_stops, 1):
        station = stop['station']
        folium.Marker(
            [station['lat'], station['lon']],
            icon=folium.Icon(color="orange", icon="bolt", prefix='fa'),
            popup=f"""
                <b>Trạm sạc #{i}: {station['name']}</b><br>
                Địa chỉ: {station['address']}<br>
                Loại: {station.get('type', 'AC/DC')} - {station.get('power', 50)}kW<br>
                Pin đến: {stop['arrival_battery']}%<br>
                Sạc từ: {stop['start_charge']}% → {stop['end_charge']}%<br>
                Thời gian sạc: {stop['charge_time']} phút
            """
        ).add_to(map_obj)

def create_comparison_chart(routes_data):
    """Tạo biểu đồ so sánh các lộ trình"""
    if not routes_data:
        return None
    
    # Cấu hình font hỗ trợ tiếng Việt
    plt.rcParams['font.family'] = 'DejaVu Sans'
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('So Sánh Các Lộ Trình', fontsize=16, fontweight='bold')
    
    route_names = [r['name'] for r in routes_data]
    colors = [r['color'] for r in routes_data]
    
    # 1. Biểu đồ tổng quãng đường
    distances = [r['metrics']['total_distance'] for r in routes_data]
    axes[0, 0].bar(route_names, distances, color=colors, alpha=0.7)
    axes[0, 0].set_title('Tong Quang Duong (km)', fontsize=12, fontweight='bold')
    axes[0, 0].set_ylabel('Km')
    for i, v in enumerate(distances):
        axes[0, 0].text(i, v + 2, f'{v:.1f}', ha='center', fontweight='bold')
    
    # 2. Biểu đồ thời gian (lái vs sạc)
    driving_times = [r['metrics']['driving_time'] for r in routes_data]
    charging_times = [r['metrics']['charging_time'] for r in routes_data]
    
    x = np.arange(len(route_names))
    width = 0.35
    
    axes[0, 1].bar(x - width/2, driving_times, width, label='Thoi gian lai', color='steelblue', alpha=0.7)
    axes[0, 1].bar(x + width/2, charging_times, width, label='Thoi gian sac', color='orange', alpha=0.7)
    axes[0, 1].set_title('Thoi Gian (phut)', fontsize=12, fontweight='bold')
    axes[0, 1].set_ylabel('Phut')
    axes[0, 1].set_xticks(x)
    axes[0, 1].set_xticklabels(route_names)
    axes[0, 1].legend()
    
    # 3. Biểu đồ số lần sạc
    num_charges = [r['metrics']['num_charges'] for r in routes_data]
    axes[1, 0].bar(route_names, num_charges, color=colors, alpha=0.7)
    axes[1, 0].set_title('So Lan Sac', fontsize=12, fontweight='bold')
    axes[1, 0].set_ylabel('Lan')
    for i, v in enumerate(num_charges):
        axes[1, 0].text(i, v + 0.1, str(v), ha='center', fontweight='bold')
    
    # 4. Biểu đồ tổng thời gian
    total_times = [r['metrics']['total_time'] for r in routes_data]
    axes[1, 1].bar(route_names, total_times, color=colors, alpha=0.7)
    axes[1, 1].set_title('Tong Thoi Gian (phut)', fontsize=12, fontweight='bold')
    axes[1, 1].set_ylabel('Phut')
    for i, v in enumerate(total_times):
        axes[1, 1].text(i, v + 5, f'{v:.0f}', ha='center', fontweight='bold')
    
    # Highlight route tốt nhất
    best_idx = 0
    for ax in axes.flat:
        if hasattr(ax, 'patches') and len(ax.patches) > 0:
            ax.patches[best_idx].set_edgecolor('red')
            ax.patches[best_idx].set_linewidth(3)
    
    plt.tight_layout()
    
    # Chuyển đổi thành base64
    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight')
    buffer.seek(0)
    image_base64 = base64.b64encode(buffer.getvalue()).decode()
    plt.close()
    
    return f"data:image/png;base64,{image_base64}"

def create_route_detail_chart(route_data):
    """Tạo biểu đồ chi tiết cho một lộ trình"""
    if not route_data['stops']:
        return None
    
    plt.rcParams['font.family'] = 'DejaVu Sans'
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(f'Chi Tiet {route_data["name"]}', fontsize=14, fontweight='bold')
    
    # 1. Biểu đồ pin qua các trạm
    stops = route_data['stops']
    station_names = [f"Tram {i+1}" for i in range(len(stops))]
    arrival_batteries = [s['arrival_battery'] for s in stops]
    departure_batteries = [s['end_charge'] for s in stops]
    
    x = np.arange(len(station_names))
    width = 0.35
    
    ax1.bar(x - width/2, arrival_batteries, width, label='Pin khi den', color='orange', alpha=0.7)
    ax1.bar(x + width/2, departure_batteries, width, label='Pin sau sac', color='green', alpha=0.7)
    ax1.axhline(y=20, color='r', linestyle='--', label='Nguong an toan')
    ax1.set_ylabel('Pin (%)')
    ax1.set_title('Trang Thai Pin Tai Cac Tram', fontweight='bold')
    ax1.set_xticks(x)
    ax1.set_xticklabels(station_names, rotation=45)
    ax1.legend()
    ax1.grid(axis='y', alpha=0.3)
    
    # 2. Biểu đồ thời gian sạc
    charge_times = [s['charge_time'] for s in stops]
    colors_gradient = plt.cm.viridis(np.linspace(0, 1, len(station_names)))
    
    ax2.barh(station_names, charge_times, color=colors_gradient, alpha=0.7)
    ax2.set_xlabel('Thoi gian (phut)')
    ax2.set_title('Thoi Gian Sac Tai Moi Tram', fontweight='bold')
    ax2.grid(axis='x', alpha=0.3)
    
    for i, v in enumerate(charge_times):
        ax2.text(v + 1, i, f'{v:.1f}', va='center', fontweight='bold')
    
    plt.tight_layout()
    
    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight')
    buffer.seek(0)
    image_base64 = base64.b64encode(buffer.getvalue()).decode()
    plt.close()
    
    return f"data:image/png;base64,{image_base64}"

@app.route('/')
def index():
    """Trang chủ"""
    m = create_map()
    add_stations_to_map(m)
    map_html = m._repr_html_()
    return render_template('index.html', map_html=map_html, ev_config=EV_CONFIG)

@app.route('/route', methods=['POST'])
def find_route():
    """API tìm đường với A* Search"""
    data = request.json
    start_addr = data.get('start')
    end_addr = data.get('end')
    avoid_highways = data.get('avoid_highways', False)
    avoid_tolls = data.get('avoid_tolls', False)
    avoid_ferries = data.get('avoid_ferries', False)
    start_battery = float(data.get('start_battery', 100))
    priority = data.get('priority', 'time')  # time, distance, charges
    
    # Cấu hình xe tùy chỉnh
    config = EV_CONFIG.copy()
    config['consumption'] = float(data.get('consumption', EV_CONFIG['consumption']))
    config['battery_capacity'] = float(data.get('battery_capacity', EV_CONFIG['battery_capacity']))
    config['min_battery'] = float(data.get('min_battery', EV_CONFIG['min_battery']))
    
    try:
        # Geocode địa chỉ
        start_loc = geolocator.geocode(start_addr + ", Việt Nam")
        end_loc = geolocator.geocode(end_addr + ", Việt Nam")
        
        if not start_loc or not end_loc:
            return jsonify({"error": "Không tìm thấy địa chỉ"}), 400
        
        start_coords = (start_loc.latitude, start_loc.longitude)
        end_coords = (end_loc.latitude, end_loc.longitude)
        
        # Bước 1: Lấy các lộ trình cơ bản
        options = {
            'avoid_highways': avoid_highways,
            'avoid_tolls': avoid_tolls,
            'avoid_ferries': avoid_ferries
        }
        
        base_routes = get_route(start_coords, end_coords, options)
        
        if not base_routes:
            return jsonify({"error": "Không tìm thấy lộ trình"}), 400
        
        # Bước 2-6: Xử lý từng lộ trình với A* Search
        routes_data = []
        
        for idx, base_route in enumerate(base_routes[:3]):  # Giới hạn 3 lộ trình
            route_coords = polyline.decode(base_route['geometry'])
            
            # Áp dụng A* Search để tìm trạm sạc tối ưu
            result, error = a_star_ev_routing(
                start_coords, 
                end_coords, 
                route_coords,
                start_battery,
                config,
                priority
            )
            
            if result:
                routes_data.append({
                    'name': f'Lộ trình {idx + 1}',
                    'route': base_route,
                    'coords': route_coords,
                    'stops': result['charging_stops'],
                    'metrics': result,
                    'color': ['blue', 'green', 'purple'][idx % 3],
                    'feasible': result['feasible']
                })
        
        if not routes_data:
            return jsonify({"error": "Không có lộ trình khả thi"}), 400
        
        # Bước 7-8: Sắp xếp theo ưu tiên
        if priority == 'time':
            routes_data.sort(key=lambda x: x['metrics']['total_time'])
        elif priority == 'distance':
            routes_data.sort(key=lambda x: x['metrics']['total_distance'])
        elif priority == 'charges':
            routes_data.sort(key=lambda x: x['metrics']['num_charges'])
        
        # Tạo bản đồ
        m = create_map(center=[start_coords[0], start_coords[1]])
        add_stations_to_map(m)
        
        for route_data in routes_data:
            label = f"{route_data['name']}: {route_data['metrics']['total_distance']} km, {route_data['metrics']['total_time']} phút"
            add_route_to_map(
                m, 
                route_data['coords'], 
                route_data['stops'],
                route_data['color'],
                label
            )
        
        # Tạo biểu đồ so sánh
        comparison_chart = create_comparison_chart(routes_data)
        
        # Tạo biểu đồ chi tiết cho từng route
        detail_charts = []
        for route_data in routes_data:
            if route_data['stops']:
                chart = create_route_detail_chart(route_data)
                detail_charts.append(chart)
            else:
                detail_charts.append(None)
        
        # Chuẩn bị dữ liệu trả về
        routes_summary = []
        for idx, route_data in enumerate(routes_data):
            routes_summary.append({
                'name': route_data['name'],
                'metrics': route_data['metrics'],
                'charging_stops': route_data['stops'],
                'feasible': route_data['feasible'],
                'detail_chart': detail_charts[idx]
            })
        
        return jsonify({
            "map": m._repr_html_(),
            "routes": routes_summary,
            "comparison_chart": comparison_chart
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
