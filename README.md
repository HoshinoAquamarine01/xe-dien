# Web Tối Ưu Lộ Trình Xe Điện

Tìm đường tốt nhất cho xe điện, tự động gợi ý trạm sạc VinFast.

## Cần chuẩn bị

- Python 3.8+
- MongoDB
- Internet

## Cài đặt

### 1. Tải mã nguồn
```bash
git clone https://github.com/HoshinoAquamarine01/xe-dien.git
cd xe-dien
```

### 2. Cài MongoDB
Tải từ: https://www.mongodb.com/try/download/community
- Chọn "Complete" và "Install as a Service"
- Kiểm tra: `sc query MongoDB`

### 3. Cài thư viện
```bash
pip install -r requirements.txt
```

### 4. Nạp dữ liệu
```bash
python import_to_mongodb.py
```

### 5. Chạy
```bash
run.bat gõ 
.\run.bat
# hoặc
gõ python app.py
```

Mở: **http://127.0.0.1:5000** dòng hiển thị sau running on

## Cách dùng

1. Nhập điểm đi và điểm đến
2. Chỉnh thông số xe (pin, dung lượng, tiêu thụ)
3. Chọn ưu tiên (thời gian/quãng đường/số lần sạc)
4. Nhấn "Tìm kiếm"

## Xem database
```bash
python view_db.py  # http://localhost:5001
```

## Sửa lỗi thường gặp

**MongoDB không chạy:**
```bash
net start MongoDB
```

**Thiếu thư viện:**
```bash
pip install -r requirements.txt
```

**Lỗi tiếng Việt:** Dùng `run.bat`

## Tác giả

Dự án môn Trí Tuệ Nhân Tạo - HCMUER 2024-2025

## License

MIT License
