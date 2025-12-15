import os
from pymongo import MongoClient
from bson import json_util  # Import thư viện xử lý đặc biệt của MongoDB

# 1. Kết nối MongoDB
client = MongoClient('mongodb+srv://lamvcdev_db_user:t2iM4x2vv14XnyFb@cluster0.fuvalf2.mongodb.net/')
db = client['ev_database']

# 2. Danh sách file
files_to_import = {
    'ev_stations.json': 'ev_stations',
    'BanDo_Pha.json': 'ferries',
    'ev_database.highway_stations.json': 'highway_stations',
    'ev_database.toll_stations.json': 'toll_stations'
}

def import_data():
    for filename, collection_name in files_to_import.items():
        if os.path.exists(filename):
            try:
                print(f"Đang xử lý file: {filename}...")
                with open(filename, 'r', encoding='utf-8') as f:
                    # SỬA LỖI: Dùng json_util.loads thay vì json.load
                    # Nó sẽ tự động parse {"$oid": "..."} thành ObjectId thực sự
                    data = json_util.loads(f.read())
                    
                    if isinstance(data, list) and len(data) > 0:
                        collection = db[collection_name]
                        
                        # Tùy chọn: Xóa dữ liệu cũ để tránh trùng lặp
                        # collection.delete_many({}) 
                        
                        # Dùng ordered=False để nếu có 1 dòng lỗi thì các dòng khác vẫn chạy tiếp
                        try:
                            result = collection.insert_many(data, ordered=False)
                            print(f"[OK] Đã import vào '{collection_name}': {len(result.inserted_ids)} bản ghi.")
                        except Exception as e:
                            # Lỗi này thường do trùng lặp _id (Duplicate Key) nếu chạy lại nhiều lần
                            print(f"[WARN] Đã import được một phần vào '{collection_name}'. Một số bản ghi bị trùng lặp hoặc lỗi.")
                            # print(e) # Bỏ comment nếu muốn xem chi tiết lỗi
                    else:
                        print(f"[SKIP] File '{filename}' rỗng hoặc không đúng định dạng list.")
            except Exception as e:
                print(f"[ERROR] Lỗi nghiêm trọng khi đọc file '{filename}': {e}")
        else:
            print(f"[MISSING] Không tìm thấy file: {filename}")

if __name__ == "__main__":
    import_data()
    print("--- Hoàn tất ---")