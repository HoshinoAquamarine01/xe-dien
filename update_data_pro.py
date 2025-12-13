"""
Script cập nhật giá tiền TOÀN DIỆN cho EV Router
Chạy: python update_data_pro.py

Tác dụng:
1. Cập nhật giá sạc EV (dựa trên power + type)
2. Cập nhật phí trạm thu phí (dựa trên tên + vị trí)
3. Cập nhật phí phà (dựa trên tên bến)
4. Đồng bộ vào cả file JSON và MongoDB
"""

import json
import os
import re
from pymongo import MongoClient

# ==============================================================================
# CẤU HÌNH
# ==============================================================================
JSON_FILES = {
    'ev_stations': 'ev_stations.json',
    'toll_stations': 'ev_database.toll_stations.json',
    'ferry_stations': 'BanDo_Pha.json'
}
DB_CONNECTION = 'mongodb://localhost:27017/'
DB_NAME = 'ev_database'

# ==============================================================================
# PHẦN 1: LOGIC TÍNH GIÁ SẠC ĐIỆN
# ==============================================================================

def calculate_charging_price(station):
    """
    Tính giá sạc điện dựa trên công suất và loại sạc (VND/kWh)
    
    Logic:
    - DC Ultra Fast (>= 60kW): 3,850 VND/kWh
    - DC Fast (20-60kW hoặc type=DC): 3,500 VND/kWh  
    - AC Level 2 (<20kW hoặc type=AC): 3,100 VND/kWh
    
    Nguồn tham khảo: Giá sạc VinFast, EVN 2024
    """
    power = station.get('power', 0)
    st_type = str(station.get('type', 'AC')).upper()
    
    # Xử lý trường hợp power là string
    if isinstance(power, str):
        numbers = re.findall(r'[\d.]+', power)
        power = float(numbers[0]) if numbers else 0
    
    # Logic phân loại
    if power >= 60:  # Sạc siêu nhanh DC (SuperCharger, V3, etc)
        return 3850
    elif 'DC' in st_type or power >= 20:  # Sạc nhanh DC thường
        return 3500
    else:  # Sạc chậm AC (Level 2)
        return 3100


# ==============================================================================
# PHẦN 2: LOGIC TÍNH PHÍ TRẠM THU PHÍ
# ==============================================================================

HIGHWAY_TOLL_FEES = {
    # Cao tốc phía Nam
    'long thành': 100000, 'dầu giây': 100000, 'long thanh': 100000, 'dau giay': 100000,
    'trung lương': 90000, 'trung luong': 90000,
    'mỹ thuận': 80000, 'my thuan': 80000,
    'bến lức': 70000, 'ben luc': 70000,
    
    # Cao tốc phía Bắc
    'hà nội': 150000, 'hải phòng': 150000, 'hai phong': 150000,
    'lào cai': 120000, 'lao cai': 120000,
    'nội bài': 85000, 'noi bai': 85000,
    'pháp vân': 40000, 'phap van': 40000,
    'cầu giẽ': 40000, 'cau gie': 40000,
    'ninh bình': 50000, 'ninh binh': 50000,
    
    # Cao tốc miền Trung
    'đà nẵng': 55000, 'da nang': 55000,
    'quảng ngãi': 55000, 'quang ngai': 55000,
    'cam lộ': 60000, 'cam lo': 60000, 'la sơn': 60000,
    
    # Hầm đường bộ
    'hầm': 110000, 'ham': 110000,
    'đèo cả': 110000, 'deo ca': 110000,
    'hải vân': 100000, 'hai van': 100000,
}

BRIDGE_TOLL_FEES = {
    'bạch đằng': 35000, 'bach dang': 35000,
    'rạch miễu': 35000, 'rach mieu': 35000,
    'cần thơ': 30000, 'can tho': 30000,
    'mỹ lợi': 25000, 'my loi': 25000,
}

def calculate_toll_fee(toll_station):
    """Tính phí cầu đường thông minh"""
    name = str(toll_station.get('name', '')).lower()
    highway = str(toll_station.get('highway', '')).lower()
    search_text = f"{name} {highway}"
    
    for keyword, fee in HIGHWAY_TOLL_FEES.items():
        if keyword in search_text:
            return fee
    
    for keyword, fee in BRIDGE_TOLL_FEES.items():
        if keyword in search_text:
            return fee
    
    if 'cao tốc' in search_text or 'expressway' in search_text:
        return 70000
    
    return 35000


# ==============================================================================
# PHẦN 3: LOGIC TÍNH PHÍ PHÀ
# ==============================================================================

FERRY_FEES = {
    # Phà biển
    'cần giờ': 190000, 'can gio': 190000,
    'vũng tàu': 190000, 'vung tau': 190000,
    'superdong': 250000,
    'cát bà': 200000, 'cat ba': 200000,
    'phú quốc': 300000, 'phu quoc': 300000,
    
    # Phà sông lớn
    'cát lái': 25000, 'cat lai': 25000,
    'bình khánh': 28000, 'binh khanh': 28000,
    'đình khao': 35000, 'dinh khao': 35000,
    'đại ngãi': 35000, 'dai ngai': 35000,
    'an phú đông': 15000, 'an phu dong': 15000,
    'thanh đa': 15000, 'thanh da': 15000,
    
    # Phà Mekong
    'vàm cống': 30000, 'vam cong': 30000,
    'cao lãnh': 30000, 'cao lanh': 30000,
    'mỹ thuận': 25000, 'my thuan': 25000,
}

def calculate_ferry_fee(ferry_station):
    """Tính phí phà dựa trên tên bến"""
    name = str(ferry_station.get('name', '')).lower()
    
    for keyword, fee in FERRY_FEES.items():
        if keyword in name:
            return fee
    
    if 'biển' in name or 'sea' in name:
        return 150000
    
    return 20000


# ==============================================================================
# PHẦN 4: HÀM XỬ LÝ DỮ LIỆU
# ==============================================================================

def process_ev_stations(stations):
    """Xử lý và thêm giá tiền vào danh sách trạm sạc"""
    stats = {'3100': 0, '3500': 0, '3850': 0}
    
    for station in stations:
        new_price = calculate_charging_price(station)
        station['price_per_kwh'] = new_price
        stats[str(new_price)] += 1
    
    print(f"   📊 Thống kê giá sạc:")
    print(f"      - 3,100 VND/kWh (AC chậm): {stats['3100']} trạm")
    print(f"      - 3,500 VND/kWh (DC nhanh): {stats['3500']} trạm")
    print(f"      - 3,850 VND/kWh (DC siêu nhanh): {stats['3850']} trạm")
    
    return len(stations)


def process_toll_stations(stations):
    """Xử lý và thêm phí cầu đường"""
    fee_ranges = {'<50k': 0, '50k-100k': 0, '>100k': 0}
    
    for station in stations:
        new_fee = calculate_toll_fee(station)
        station['fee'] = new_fee
        
        if new_fee < 50000:
            fee_ranges['<50k'] += 1
        elif new_fee <= 100000:
            fee_ranges['50k-100k'] += 1
        else:
            fee_ranges['>100k'] += 1
    
    print(f"   📊 Thống kê phí toll:")
    print(f"      - Dưới 50k (BOT thường): {fee_ranges['<50k']} trạm")
    print(f"      - 50k-100k (Cao tốc): {fee_ranges['50k-100k']} trạm")
    print(f"      - Trên 100k (Cao tốc đắt): {fee_ranges['>100k']} trạm")
    
    return len(stations)


def process_ferry_stations(stations):
    """Xử lý và thêm phí phà"""
    fee_ranges = {'<30k': 0, '30k-100k': 0, '>100k': 0}
    
    for station in stations:
        new_fee = calculate_ferry_fee(station)
        station['fee'] = new_fee
        
        if new_fee < 30000:
            fee_ranges['<30k'] += 1
        elif new_fee <= 100000:
            fee_ranges['30k-100k'] += 1
        else:
            fee_ranges['>100k'] += 1
    
    print(f"   📊 Thống kê phí phà:")
    print(f"      - Dưới 30k (Phà nhỏ): {fee_ranges['<30k']} trạm")
    print(f"      - 30k-100k (Phà lớn): {fee_ranges['30k-100k']} trạm")
    print(f"      - Trên 100k (Phà biển): {fee_ranges['>100k']} trạm")
    
    return len(stations)


# ==============================================================================
# PHẦN 5: ĐỌC/GHI FILE VÀ MONGODB
# ==============================================================================

def update_json_file(file_path, processor_func):
    """Đọc, xử lý và ghi lại file JSON"""
    if not os.path.exists(file_path):
        print(f"   ⚠️ File không tồn tại: {file_path}")
        return None
    
    print(f"🔄 Đang đọc: {file_path}...")
    
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"   📦 Tìm thấy {len(data)} records")
    updated_count = processor_func(data)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"   ✅ Đã cập nhật {updated_count} records vào file JSON\n")
    return data


def sync_to_mongodb(collection_name, data):
    """Đồng bộ dữ liệu vào MongoDB"""
    if not data:
        return False
    
    print(f"🔄 Đang đồng bộ vào MongoDB: {collection_name}...")
    
    try:
        client = MongoClient(DB_CONNECTION, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        
        db = client[DB_NAME]
        collection = db[collection_name]
        
        # Xóa _id để tránh lỗi duplicate key
        for doc in data:
            if '_id' in doc:
                del doc['_id']
        
        old_count = collection.count_documents({})
        collection.delete_many({})
        
        if data:
            result = collection.insert_many(data)
            new_count = len(result.inserted_ids)
            print(f"   ✅ Đã thay thế {old_count} → {new_count} documents")
        
        client.close()
        return True
        
    except Exception as e:
        print(f"   ❌ Lỗi MongoDB: {e}")
        return False


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    print("=" * 70)
    print("🔋 SCRIPT CẬP NHẬT GIÁ TIỀN TOÀN DIỆN CHO EV ROUTER")
    print("=" * 70)
    print()
    
    # 1. EV Stations
    print("📦 PHẦN 1: TRẠM SẠC ĐIỆN")
    print("-" * 50)
    ev_data = update_json_file(JSON_FILES['ev_stations'], process_ev_stations)
    if ev_data:
        sync_to_mongodb('ev_stations', ev_data)
    
    # 2. Toll Stations
    print("📦 PHẦN 2: TRẠM THU PHÍ")
    print("-" * 50)
    toll_data = update_json_file(JSON_FILES['toll_stations'], process_toll_stations)
    if toll_data:
        sync_to_mongodb('toll_stations', toll_data)
    
    # 3. Ferry Stations
    print("📦 PHẦN 3: BẾN PHÀ")
    print("-" * 50)
    ferry_data = update_json_file(JSON_FILES['ferry_stations'], process_ferry_stations)
    if ferry_data:
        sync_to_mongodb('ferry_stations', ferry_data)
    
    print()
    print("=" * 70)
    print("✅ HOÀN TẤT!")
    print("=" * 70)
    print()
    print("📝 Bảng giá đã áp dụng:")
    print("   ⚡ Sạc: 3,100 (AC) / 3,500 (DC) / 3,850 (DC Ultra Fast)")
    print("   🚧 Toll: 35k (BOT) / 70k-150k (Cao tốc)")
    print("   🚢 Phà: 20k (nhỏ) / 25-35k (lớn) / 190k+ (biển)")
    print()
    print("🔄 Khởi động lại app.py để áp dụng!")
    print()


if __name__ == "__main__":
    main()
