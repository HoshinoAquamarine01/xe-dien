# Web giúp tối ưu lộ trình cho xe điện

Ứng dụng web tìm kiếm lộ trình tối ưu cho xe điện với các trạm sạc VinFast.

## Yêu cầu hệ thống

- Python 3.8+
- MongoDB Community Server
- Trình duyệt web hiện đại

## Cài đặt

### Bước 1: cài đặt MongoDB

Tải và cài đặt MongoDB Community Server:
- Link: https://www.mongodb.com/try/download/community
- Chọn "Install MongoDB as a Service"
- Port mặc định: 27017

### Bước 2: cài đặt thư viện cần thiết cho Python

```bash
pip install -r requirements.txt
```

### Bước 3: import dữ liệu trạm sạc vào MongoDB

```bash
python import_to_mongodb.py
```

##  Bước 4: chạy chương trình

### cách 1 có thể dùng file batch để chạy tự động
```bash
run.bat
```

### cách 2 dùng python trực tiếp
```bash
python app.py
```

Mở trình duyệt và truy cập để kiểm tra **http://localhost:5000**

## Để kiểm tra database

Chạy database viewer:
```bash
python view_db.py
```

Truy cập: **http://localhost:5001**

## Hướng dẫn sử dụng cơ bản cho người mới

1. Nhập điểm xuất phát và điểm đến
2. Điều chỉnh thông số xe điện (pin, tiêu thụ, dung lượng)
3. Chọn tùy chọn ưu tiên (thời gian, quãng đường, số lần sạc)
4. Nhấn "Tìm kiếm"
5. Xem lộ trình tối ưu với các trạm sạc được đề xuất

### Cấu  cho xe điện của bạn

Chỉnh sửa thông số xe điện trong `app.py`:

```python
EV_CONFIG = {
    "battery_capacity": 75,      # kWh
    "consumption": 16.3,         # kWh/100km
    "max_range": 460,            # km
    "charge_speed_fast": 150,    # kW (DC Fast)
    "charge_speed_slow": 50,     # kW (AC)
    "min_battery": 20,           # % ngưỡng an toàn
    "charge_target": 80          # % mục tiêu sạc
}
```

## Các lưu ý và lỗi thường gặp

- Đảm bảo MongoDB service đang chạy
- Cần kết nối internet để sử dụng OSRM API và geocoding
- Dữ liệu trạm sạc chỉ mang tính chất minh họa

## Tác giả

Phát triển bởi HCMUER - phục vụ cho môn học trí tuệ nhân tạo

## License

MIT License
