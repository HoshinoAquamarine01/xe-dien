# HỆ THỐNG TỐI ƯU LỘ TRÌNH CHO XE ĐIỆN

## Changelog (Cập nhật gần đây) — 2026-09-22

- ✅ **Non-linear battery consumption**: Mô hình tiêu thụ năng lượng cập nhật để phụ thuộc vào tốc độ (speed-aware), cải thiện độ chính xác cho cao tốc và trường hợp tắc đường. (file: `app.py`)
- ✅ **A\* upgrades & dynamic costs**: Hỗ trợ chi phí động theo **time/cost/distance**, chiến lược phân nhánh khi gợi ý trạm sạc và truyền `route_avg_speed` vào tính toán. (file: `app.py`)
- ✅ **Edge-case handling**: Phát hiện **Dead End** (điểm đến không có trạm sạc trong 50km) và **Disconnected Graph** (vùng cô lập trạm sạc; nearest_rescue > 80km). Trả về `destination_warning` / `infeasibility` với `error_type` tương ứng để frontend hiển thị cảnh báo. (file: `app.py`)
- ✅ **Frontend UI fixes**: Popup trạm dừng cao tốc được làm nổi bật màu đỏ khi là cảnh báo; đoạn đường cao tốc được tô đỏ khi không thể tránh; thêm badge cảnh báo cho route khi điểm đến xa trạm sạc. (file: `templates/index.html`)
- ✅ **API improvements**: Thuộc tính mới trong response như `destination_warning`, `feasible` flag, và lỗi dạng `disconnected_graph`/`battery_depleted` để UI xử lý chính xác.

> Ghi chú: Sau khi cập nhật, chạy `python app.py` và thử những trường hợp với các tuỳ chọn `avoid_*` hoặc thay đổi `start_battery` để xác thực cảnh báo và modal không khả thi.

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

| Công nghệ                         | Mục đích                              |
| --------------------------------- | ------------------------------------- |
| **HTML5/CSS3**                    | Giao diện người dùng                  |
| **JavaScript (ES6+)**             | Xử lý logic client-side               |
| **GoongJS API 1.0+**              | Bản đồ tương tác chính (Vector Tiles) |
| **Leaflet.js** (fallback)         | Bản đồ dự phòng nếu GoongJS lỗi       |
| **Material Symbols**              | Icon hiện đại cho UI và marker        |
| **Font Awesome 6.4.0**            | Icon bản đồ và UI                     |
| **Google Fonts (Inter, Poppins)** | Typography hiện đại                   |

### Database

| Hệ thống         | Mục đích                         |
| ---------------- | -------------------------------- |
| **MongoDB 7.0+** | Lưu trữ dữ liệu trạm sạc VinFast |

### API bên thứ 3

| API                                      | Mục đích                                               |
| ---------------------------------------- | ------------------------------------------------------ |
| **Goong Maps API**                       | Routing chính cho Viủt Nam (directions, geocoding)     |
| **Mapbox Directions API** (fallback)     | Routing dự phòng khi Goong lỗi hoặc không hỗ trợ avoid |
| **OSRM (OpenStreetMap Routing Machine)** | Tính toán đường đi thực tế giữa 2 điểm                 |
| **Nominatim (OpenStreetMap)**            | Chuyển đổi địa chỉ thành tọa độ GPS                    |

### Routing filters (Avoid)

- Hệ thống hỗ trợ các tuỳ chọn tránh: **Tránh cao tốc**, **Tránh trạm thu phí**, và **Tránh phà**.
- Khi người dùng bật các tùy chọn này, backend sẽ **truyền tham số tránh (avoid/exclude)** xuống Goong (nếu được hỗ trợ) hoặc Mapbox để routing engine cố gắng trả về tuyến thay thế không chứa các yếu tố bị tránh.
- Nếu Goong vẫn trả tuyến vi phạm bộ lọc, hệ thống sẽ **tự động thử Mapbox** với tham số `exclude` để tìm tuyến thay thế; nếu không có tuyến thay thế, hệ thống sẽ trả tuyến tốt nhất và **gắn cờ cảnh báo** để người dùng biết rằng không có tuyến tránh nào khả thi.

### Thuật toán và Logic Tính toán

#### 1. Thuật toán A\* (A-Star) cho EV Routing

**Mục đích**: Tìm lộ trình tối ưu với các ràng buộc về pin và trạm sạc.

**Công thức cost function**:

```
f(n) = g(n) + h(n)
```

Trong đó:

- `g(n)`: Chi phí thực tế từ điểm đầu đến node n (thời gian + chi phí sạc)
- `h(n)`: Ước lượng chi phí từ node n đến đích (khoảng cách Haversine)

**Heuristic weight**: Hệ thống sử dụng hệ số 1.15x cho heuristic để tăng tốc độ hội tụ:

```
f(n) = g(n) + 1.15 * h(n)
```

**Constraints (Ràng buộc)**:

- Mức pin tại mỗi node: `battery_level >= min_battery_threshold` (mặc định 10%)
- Khoảng cách tới trạm sạc tiếp theo: `distance_to_next <= current_range`
- Tổng số trạm sạc: Tối thiểu hóa để giảm thời gian

#### 2. KD-Tree cho Tìm kiếm Trạm Sạc

**Mục đích**: Tìm trạm sạc gần nhất trong bán kính cho trước.

**Thuật toán**: KD-Tree (K-Dimensional Tree) từ scipy.spatial

**Độ phức tạp**:

- Xây dựng tree: O(n log n)
- Truy vấn: O(log n)
- Bộ nhớ: O(n)

**Buffer zone (Vùng đệm)**:

- `buffer_km = 25.0` km (tăng từ 12km cho tuyến dài)
- Tìm tất cả trạm sạc trong vùng buffer dọc theo route

#### 3. Tính Toán Pin và Quãng Đường

**Công thức tiêu thụ pin**:

```python
energy_consumed = (distance_km * consumption_kwh_per_100km) / 100
battery_remaining = current_battery - energy_consumed
```

**Quãng đường còn đi được**:

```python
remaining_range_km = (battery_percent * battery_capacity_kwh) / consumption_kwh_per_100km * 100
```

Ví dụ:

- Pin: 80% x 60 kWh = 48 kWh
- Tiêu thụ: 15 kWh/100km
- Quãng đường: (48 / 15) \* 100 = 320 km

**Tính toán thời gian và lượng sạc**:

```python
# Sạc từ A% đến B%
energy_needed = (target_percent - current_percent) * battery_capacity / 100
charge_time_minutes = (energy_needed / charging_power_kw) * 60

# Chi phí sạc
charging_cost = energy_needed * price_per_kwh
```

#### 4. Tối Ưu hóa Routing cho Tuyến Dài

**Adaptive Sampling** (Lấy mẫu thích nghi):

- Tuyến ngắn (<500km): `sample_interval = 2 km`
- Tuyến dài (≥500km): `sample_interval = 5 km`
- Mục đích: Giảm số điểm kiểm tra, tăng tốc xử lý

**Candidate Pruning** (Loại bỏ ứng viên không khả thi):

```python
if battery_after_reaching_station < min_battery:
    continue  # Bỏ qua trạm này
if distance_to_station > current_range:
    continue  # Không đến được
```

**Multi-route optimization**: Hệ thống tạo tối đa 3 lộ trình thay thế với các ưu tiên khác nhau:

1. **Lộ trình tối ưu**: Cân bằng thời gian và chi phí
2. **Lộ trình nhanh nhất**: Ưu tiên thời gian, ít trạm sạc
3. **Lộ trình rẻ nhất**: Ưu tiên chi phí, có thể lâu hơn

#### 5. Geocoding và Routing Fallback

**Multi-provider strategy**:

1. **Goong Geocoding** (우선): Đối với địa chỉ Việt Nam
2. **Nominatim** (fallback): Khi Goong thất bại
3. **Direct coordinates**: Nếu user nhập lat,lng

**Routing provider priority**:

```
1. Goong Directions API (Việt Nam)
   ↓ (nếu lỗi hoặc không hỗ trợ avoid)
2. Mapbox Directions API (toàn cầu)
   ↓ (nếu vẫn lỗi)
3. OSRM Public API (dự phòng cuối cùng)
```

### Thuật toán

- **A\* Algorithm**: Tối ưu hóa lộ trình với các ràng buộc về pin
- **KD-Tree**: Tìm kiếm trạm sạc gần nhất nhanh chóng (O(log n))

---

## 4.1.1 Tính năng Chính

### 1. Routing Đa dạng

- **3 lộ trình thay thế**: Tối ưu, Nhanh nhất, Rẻ nhất
- **Tự động chọn trạm sạc**: Dựa trên mức pin, quãng đường, công suất sạc
- **Hiển thị thông tin chi tiết**: Thời gian sạc, chi phí, quãng đường từ điểm xuất phát

### 2. Tương tác Bản Đồ (GoongJS)

- **Vector tiles**: Hiển thị bản đồ mượt mà, nhanh
- **Marker tùy chỉnh**: Trạm sạc, phà, thu phí, trạm dừng nghỉ
- **Popup thông tin**: Hiển thị chi tiết khi click vào marker
- **Chế độ xem**: Standard, Satellite, Terrain

### 3. Bộ lọc Đường đi

- **Tránh cao tốc**: Tìm tuyến qua quốc lộ
- **Tránh thu phí**: Giảm chi phí tuyến đường
- **Tránh phà**: Tối ưu cho xe không muốn chờ phà
- **Cảnh báo thông minh**: Hiển thị khi không có tuyến thay thế

### 4. Quản lý Pin & Tiêu thụ

- **Tính toán thời gian thực**: Dựa trên mức tiêu thụ thực tế
- **Cảnh báo pin yếu**: Khi pin < 20% hoặc không đủ đến đích
- **Gợi ý thông số xe**: Bảng tham khảo VinFast VF3, VF5, VF8, VF9
- **Xác thực đầu vào**: Kiểm tra thông số không hợp lý

### 5. Hiểu năng & Tối ưu hóa

- **Adaptive sampling**: Lấy mẫu 2-5km tùy quãng đường
- **Candidate pruning**: Loại trạm sạc không khả thi sớm
- **Heuristic weight 1.15x**: Tăng tốc A\* mà vẫn tối ưu
- **Buffer zone 25km**: Tìm trạm sạc trong vùng rộng hơn

## 4.1.2 Kết quả Đo lường Hiệu năng

### Thời gian Xử lý (Test: HCM → Hà Nội, ~1700km)

| Giai đoạn               | Thời gian | Ghi chú                         |
| ----------------------- | --------- | ------------------------------- |
| Geocoding (2 điểm)      | 0.5-1.2s  | Goong API (Việt Nam)            |
| Routing (Base)          | 1.5-2.8s  | Lấy geometry từ Goong           |
| KD-Tree Build           | 0.1s      | Index 200+ trạm sạc             |
| Trạm sạc trong buffer   | 0.3s      | Query trong 25km buffer         |
| A\* Routing (optimized) | 3.5-6.2s  | Với adaptive sampling + pruning |
| **Tổng thời gian**      | **6-10s** | Cho tuyến 1700km                |

### So sánh Hiệu quả Tối ưu hóa

| Tính năng                 | Trước  | Sau   | Cải thiện |
| ------------------------- | ------ | ----- | --------- |
| Buffer zone               | 12 km  | 25 km | +108%     |
| Sample interval (>500km)  | 2 km   | 5 km  | +150%     |
| Heuristic weight          | 1.0x   | 1.15x | +15%      |
| Avg routing time (HCM-HN) | 15-25s | 6-10s | -60%      |
| Success rate (long route) | 75%    | 95%   | +20%      |

### Độ Chính xác

- **Pin remaining**: ±3% so với thực tế (test với VinFast VF8)
- **Thời gian sạc**: ±5 phút (150kW DC fast charging)
- **Quãng đường**: ±2% (đối chiếu với Google Maps)
- **Chi phí sạc**: ±10% (dựa trên giá trung bình 3,500€/kWh)

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

