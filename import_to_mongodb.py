import os
import json
import sys
from pymongo import MongoClient
from dotenv import load_dotenv

# --- CẤU HÌNH ---
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')

# Định nghĩa các file và collection tương ứng
FILES_MAPPING = {
    'ev_stations.json': 'ev_stations',
    'BanDo_Pha.json': 'ferries',
    'ev_database.highway_stations.json': 'highway_stations',
    'ev_database.toll_stations.json': 'toll_stations',
    'ev_database.highways.json': 'highways'
}

def connect_db():
    if not MONGO_URI:
        print("❌ LỖI: Không tìm thấy biến môi trường MONGO_URI.")
        sys.exit(1)
    
    try:
        client = MongoClient(MONGO_URI)
        client.admin.command('ping')
        print("✅ Kết nối MongoDB thành công!")
        return client['ev_database']
    except Exception as e:
        print(f"❌ Lỗi kết nối MongoDB: {e}")
        sys.exit(1)

def standardize_record(record):
    """
    Hàm chuẩn hóa dữ liệu:
    1. XÓA BỎ _id cũ để tránh lỗi $oid
    2. Tìm và ép kiểu tọa độ lat/lon
    """
    # [FIX QUAN TRỌNG] Xóa _id cũ để MongoDB tự tạo mới
    if '_id' in record:
        del record['_id']

    # 1. Xử lý Latitude
    lat = record.get('lat')
    if lat is None: lat = record.get('latitude')
    
    # 2. Xử lý Longitude
    lon = record.get('lon')
    if lon is None: lon = record.get('longitude')

    # 3. Kiểm tra tính hợp lệ
    try:
        if lat is None or lon is None:
            return None # Bỏ qua bản ghi rác (null)
        
        # Ép kiểu float
        lat_val = float(lat)
        lon_val = float(lon)
        
        # Kiểm tra range hợp lý (Việt Nam)
        if not (-90 <= lat_val <= 90) or not (-180 <= lon_val <= 180):
            return None

        # Gán lại giá trị chuẩn
        record['lat'] = lat_val
        record['lon'] = lon_val
        
        return record
    except ValueError:
        return None 

def import_file(db, filename, collection_name):
    if not os.path.exists(filename):
        print(f"⚠️  BỎ QUA: Không tìm thấy file '{filename}'")
        return

    print(f"\n📂 Đang xử lý: {filename} -> Collection: {collection_name}")
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
            
        if not isinstance(raw_data, list):
            print(f"   ❌ Lỗi: File không phải là danh sách (List).")
            return

        valid_data = []
        invalid_count = 0

        # Duyệt và chuẩn hóa
        for item in raw_data:
            clean_item = standardize_record(item)
            if clean_item:
                valid_data.append(clean_item)
            else:
                invalid_count += 1

        # Import vào DB
        collection = db[collection_name]
        collection.delete_many({}) # Reset cũ
        print(f"   🗑️  Đã xóa dữ liệu cũ.")

        if valid_data:
            # Insert
            result = collection.insert_many(valid_data)
            print(f"   ✅ Đã thêm mới: {len(result.inserted_ids)} bản ghi.")
            
            if invalid_count > 0:
                print(f"   ⚠️  Phát hiện {invalid_count} bản ghi lỗi (thiếu lat/lon hoặc sai định dạng).")
                if collection_name == 'highway_stations':
                    print("      -> Lưu ý: File highway_stations của bạn có vẻ chứa toàn dữ liệu rỗng (null).")
        else:
            print("   ⚠️  Không có dữ liệu hợp lệ nào để thêm.")
            if invalid_count > 0:
                print(f"      (Đã lọc bỏ {invalid_count} bản ghi lỗi hoàn toàn)")

    except Exception as e:
        print(f"   ❌ Lỗi xử lý file: {e}")

if __name__ == "__main__":
    import_file(connect_db(), 'dummy', 'dummy') # Init check
    db = connect_db()
    for fname, cname in FILES_MAPPING.items():
        import_file(db, fname, cname)
    print("\n=== HOÀN TẤT ===")