# HỆ THỐNG TỐI ƯU LỘ TRÌNH CHO XE ĐIỆN

## 4.1 Công cụ và Thư viện Sử dụng

### Backend (Python Flask)

| Thư viện       | Phiên bản | Mục đích sử dụng                         |
| -------------- | --------- | ---------------------------------------- |
| **Flask**      | 3.0.0+    | Framework web chính để xây dựng REST API |
| **pymongo**    | 4.6.0+    | Kết nối và truy vấn MongoDB database     |
| **folium**     | 0.15.0+   | Tạo bản đồ tương tác với OpenStreetMap   |
| **geopy**      | 2.4.1+    | Geocoding (chuyển địa chỉ thành tọa độ)  |
| **requests**   | 2.31.0+   | Gọi API OSRM để lấy đường đi             |
| **polyline**   | 2.0.0+    | Decode geometry từ OSRM API              |
| **matplotlib** | 3.8.0+    | Vẽ biểu đồ so sánh và chi tiết           |
| **numpy**      | 1.26.0+   | Tính toán số học, xử lý mảng             |
| **scipy**      | 1.11.0+   | KD-Tree để tìm trạm sạc gần nhất         |

### Frontend

| Công nghệ                         | Mục đích                |
| --------------------------------- | ----------------------- |
| **HTML5/CSS3**                    | Giao diện người dùng    |
| **JavaScript (ES6+)**             | Xử lý logic client-side |
| **Font Awesome 6.4.0**            | Icon bản đồ và UI       |
| **Google Fonts (Inter, Poppins)** | Typography hiện đại     |

### Database

| Hệ thống         | Mục đích                         |
| ---------------- | -------------------------------- |
| **MongoDB 7.0+** | Lưu trữ dữ liệu trạm sạc VinFast |

### API bên thứ 3

| API                                      | Mục đích                               |
| ---------------------------------------- | -------------------------------------- |
| **OSRM (OpenStreetMap Routing Machine)** | Tính toán đường đi thực tế giữa 2 điểm |
| **Nominatim (OpenStreetMap)**            | Chuyển đổi địa chỉ thành tọa độ GPS    |

### Thuật toán

- **A\* Algorithm**: Tối ưu hóa lộ trình với các ràng buộc về pin
- **KD-Tree**: Tìm kiếm trạm sạc gần nhất nhanh chóng (O(log n))

---

## 4.2 Cấu hình Môi trường

### 1. Yêu cầu Hệ thống

- **Hệ điều hành**: Windows 10/11, macOS 10.15+, Linux (Ubuntu 20.04+)
- **Python**: 3.9 trở lên
- **MongoDB**: 7.0 trở lên
- **RAM**: Tối thiểu 4GB (Khuyến nghị 8GB)
- **Dung lượng ổ cứng**: 500MB trống

### 2. Cài đặt Python và Pip

#### Windows:

```bash
# Tải Python từ https://www.python.org/downloads/
# Chọn "Add Python to PATH" khi cài đặt

# Kiểm tra cài đặt
python --version
pip --version
```

#### macOS:

```bash
# Sử dụng Homebrew
brew install python@3.11

# Kiểm tra
python3 --version
pip3 --version
```

#### Linux (Ubuntu/Debian):

```bash
sudo apt update
sudo apt install python3.11 python3-pip

# Kiểm tra
python3 --version
pip3 --version
```

### 3. Cài đặt MongoDB

#### Windows:

```bash
# Tải từ https://www.mongodb.com/try/download/community
# Chạy installer và chọn "Install MongoDB as a Service"

# Kiểm tra dịch vụ
net start MongoDB
```

#### macOS:

```bash
# Sử dụng Homebrew
brew tap mongodb/brew
brew install mongodb-community@7.0

# Khởi động MongoDB
brew services start mongodb-community@7.0
```

#### Linux (Ubuntu):

```bash
# Import MongoDB public GPG key
wget -qO - https://www.mongodb.org/static/pgp/server-7.0.asc | sudo apt-key add -

# Tạo list file
echo "deb [ arch=amd64,arm64 ] https://repo.mongodb.org/apt/ubuntu focal/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list

# Cài đặt MongoDB
sudo apt update
sudo apt install -y mongodb-org

# Khởi động MongoDB
sudo systemctl start mongod
sudo systemctl enable mongod
```

### 4. Clone Project

```bash
# Clone repository
git clone <repository-url>
cd xe-dien

# Hoặc tải zip và giải nén vào thư mục mong muốn
```

### 5. Tạo Virtual Environment

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python3 -m venv venv
source venv/bin/activate
```

### 6. Cài đặt Dependencies

```bash
# Cài đặt tất cả thư viện từ requirements.txt
pip install -r requirements.txt

# Hoặc cài đặt thủ công từng thư viện:
pip install flask pymongo folium geopy requests polyline matplotlib numpy scipy
```

#### Tạo file `requirements.txt`:

```txt
Flask==3.0.0
pymongo==4.6.0
folium==0.15.0
geopy==2.4.1
requests==2.31.0
polyline==2.0.0
matplotlib==3.8.2
numpy==1.26.2
scipy==1.11.4
```

### 7. Import Dữ liệu vào MongoDB

```bash
# Khởi động MongoDB
mongod --dbpath <đường-dẫn-data>

# Mở terminal mới, import dữ liệu
mongoimport --db ev_database --collection ev_stations --file data/stations.json --jsonArray

# Kiểm tra import thành công
mongosh
use ev_database
db.ev_stations.countDocuments()  # Phải trả về > 0
```

### 8. Cấu hình Biến Môi trường (Tùy chọn)

Tạo file `.env` trong thư mục gốc:

```env
# MongoDB
MONGODB_URI=mongodb://localhost:27017/
DB_NAME=ev_database
COLLECTION_NAME=ev_stations

# Flask
FLASK_APP=app.py
FLASK_ENV=development
FLASK_DEBUG=True
SECRET_KEY=your-secret-key-here

# Server
HOST=0.0.0.0
PORT=5000

# API Keys (nếu sử dụng API có key)
OSRM_API_KEY=
NOMINATIM_USER_AGENT=ev-app-hcm
```

### 9. Khởi động Ứng dụng

```bash
# Đảm bảo virtual environment đã kích hoạt
# Khởi động Flask server
python app.py

# Hoặc sử dụng Flask CLI
flask run
```

Truy cập: **http://localhost:5000**

### 10. Kiểm tra Cài đặt

#### Test MongoDB Connection:

```python
from pymongo import MongoClient

client = MongoClient('mongodb://localhost:27017/')
db = client['ev_database']
print(f"Số trạm sạc: {db.ev_stations.count_documents({})}")
```

#### Test API Endpoint:

```bash
curl http://localhost:5000/
# Phải trả về trang HTML
```

### 11. Cấu trúc Thư mục

```
xe-dien/
├── app.py                 # File chính của Flask app
├── requirements.txt       # Dependencies Python
├── README.md             # File này
├── .env                  # Biến môi trường (không commit)
├── .gitignore           # Ignore files
├── templates/
│   └── index.html       # Giao diện web chính
├── static/
│   ├── avt.ico          # Favicon
│   └── css/             # CSS tùy chỉnh (nếu có)
└── data/
    └── stations.json    # Dữ liệu trạm sạc VinFast
```

### 12. Troubleshooting

#### Lỗi MongoDB không kết nối được:

```bash
# Kiểm tra MongoDB đang chạy
# Windows
net start MongoDB

# macOS
brew services list | grep mongodb

# Linux
sudo systemctl status mongod
```

#### Lỗi thiếu thư viện:

```bash
# Cài đặt lại tất cả dependencies
pip install --upgrade -r requirements.txt
```

#### Lỗi Port 5000 đã được sử dụng:

```python
# Trong app.py, đổi port:
if __name__ == '__main__':
    app.run(debug=True, port=5001, host='0.0.0.0')
```

#### Lỗi OSRM API timeout:

- Kiểm tra kết nối Internet
- Server OSRM public có thể bị quá tải
- Thử lại sau vài phút

### 13. Production Deployment (Tùy chọn)

#### Sử dụng Gunicorn:

```bash
pip install gunicorn

# Chạy với 4 workers
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

#### Sử dụng Docker:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["python", "app.py"]
```

---

## Liên hệ & Hỗ trợ

- **Email**: support@example.com
- **Issues**: GitHub Issues
- **Documentation**: [Link tài liệu]

---

**Phiên bản**: 1.0.0  
**Ngày cập nhật**: 2024-01-XX  
**Tác giả**: [Tên nhóm/cá nhân]
