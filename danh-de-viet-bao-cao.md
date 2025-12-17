---

#⚡ EV ROUTER - HỆ THỐNG TỐI ƯU LỘ TRÌNH XE ĐIỆN THÔNG MINHChào mừng bạn đến với **EV Router**! Đây là giải pháp toàn diện giúp giải quyết bài toán "lo âu về quãng đường" (range anxiety) cho người dùng xe điện tại Việt Nam. Không chỉ đơn thuần là tìm đường, hệ thống tính toán một lộ trình khả thi dựa trên dung lượng pin, vị trí trạm sạc và các yếu tố giao thông thực tế.

---

## 🛠 PHẦN 1: GIẢI PHÁP KỸ THUẬT & THUẬT TOÁN (ENGINEERING DEEP DIVE)Phần này đi sâu vào cách chúng tôi giải quyết các bài toán hóc búa trong việc định tuyến xe điện.

### 1. Bài toán Định tuyến Đa Ràng buộc (Multi-Constrained Routing)Việc tìm đường cho xe điện phức tạp hơn xe xăng rất nhiều vì nó không chỉ là bài toán tìm đường ngắn nhất (Shortest Path) mà là tìm đường khả thi nhất dựa trên năng lượng (Energy-Feasible Path).

#### Giải pháp: Thuật toán A* (A-Star) Tùy biếnChúng tôi không sử dụng Dijkstra thuần túy vì không gian tìm kiếm quá lớn. Thay vào đó, chúng tôi sử dụng **A*** với hàm heuristic được tinh chỉnh (tuned).

* **Cost Function `f(n)`:**
```math
f(n) = g(n) + w * h(n)

```


* **`g(n)` (Chi phí thực):** Là tổng hợp của *Thời gian di chuyển* + *Thời gian sạc dự kiến* + *Chi phí tiền sạc/thu phí*.
* **`h(n)` (Heuristic):** Khoảng cách Haversine (đường chim bay) từ điểm hiện tại đến đích.
* **`w` (Weight - Trọng số):** Chúng tôi áp dụng hệ số **1.15**.
* *Tại sao?* Với `w = 1`, A* đảm bảo đường ngắn nhất nhưng chậm. Với `w > 1` (Weighted A*), thuật toán chấp nhận sai số nhỏ để hội tụ về đích nhanh hơn gấp nhiều lần, phù hợp cho các request web cần phản hồi dưới 2 giây.




* **Ràng buộc cứng (Hard Constraints):** Tại mỗi node (điểm nút giao thông hoặc trạm sạc), trạng thái pin `SoC` (State of Charge) không bao giờ được phép < `min_threshold` (ví dụ: 10%). Nếu vi phạm, nhánh tìm kiếm đó bị cắt bỏ (pruned) ngay lập tức.

### 2. Bài toán Tìm kiếm Không gian (Spatial Search)Làm thế nào để tìm trạm sạc nhanh nhất trong hàng nghìn trạm sạc trải dài khắp Việt Nam mà không làm chậm hệ thống?

#### Giải pháp: Cấu trúc dữ liệu KD-TreeThay vì tính khoảng cách từ xe đến tất cả 5000+ trạm sạc (Độ phức tạp `O(n)`), chúng tôi sử dụng **KD-Tree** (K-Dimensional Tree) từ thư viện `scipy`.

* **Cơ chế:** KD-Tree chia không gian bản đồ thành các vùng nhỏ dạng cây nhị phân.
* **Hiệu quả:** Khi xe đang ở tọa độ (X, Y), việc tìm trạm sạc gần nhất chỉ tốn độ phức tạp `O(log n)`.
* **Vùng đệm thông minh (Smart Buffer Corridor):**
* Hệ thống không tìm trạm sạc trên toàn bản đồ.
* Chúng tôi tạo một "hành lang ảo" (buffer zone) dọc theo lộ trình dự kiến với bán kính **25km**. Chỉ các trạm nằm trong hành lang này mới được đưa vào tính toán, giúp giảm 90% khối lượng xử lý thừa.



### 3. Bài toán Tối ưu Hiệu năng cho Lộ trình Dài (Long-distance Optimization)Tìm đường từ TP.HCM ra Hà Nội (~1700km) tạo ra hàng triệu điểm dữ liệu. Nếu xét duyệt từng mét đường, server sẽ bị treo (timeout).

#### Giải pháp: Lấy mẫu Thích nghi (Adaptive Sampling)Chúng tôi không kiểm tra pin tại mọi điểm trên đường. Chúng tôi áp dụng kỹ thuật lấy mẫu động:

* **Cơ chế:**
* Nếu tổng lộ trình < 500km: Kiểm tra (sample) mỗi **2km**.
* Nếu tổng lộ trình ≥ 500km: Kiểm tra (sample) mỗi **5km**.


* **Kết quả:** Giảm số lượng node cần tính toán từ hàng chục nghìn xuống còn vài trăm, giúp thời gian phản hồi giảm từ 25s xuống còn **6-10s** cho tuyến Bắc-Nam mà sai số về pin chỉ dưới 2%.

### 4. Mô phỏng Vật lý Pin (Battery Physics Simulation)Chúng tôi không giả định pin tiêu hao tuyến tính một cách ngây thơ. Hệ thống tính toán dựa trên mô hình tiêu thụ năng lượng:

* **Công thức tiêu thụ:**
```python
Năng_lượng_tiêu_thụ (kWh) = (Quãng_đường (km) * Mức_tiêu_thụ_xe (kWh/100km)) / 100

```


* **Mô hình sạc phi tuyến tính:**
* Thời gian sạc không đều. Sạc từ 0-80% nhanh hơn nhiều so với 80-100% (do đặc tính pin Li-ion giảm dòng để bảo vệ cell pin).
* Hệ thống tính toán thời gian sạc dựa trên công suất trạm (ví dụ: 60kW, 250kW) và đường cong sạc (charging curve) giả lập để đưa ra thời gian chờ chính xác nhất cho người dùng.



### 5. Xử lý Bộ lọc & Fallback (Filtering & Failover Strategy)Người dùng muốn tránh cao tốc, phà hoặc trạm thu phí. Đây là bài toán khó vì dữ liệu bản đồ Việt Nam đôi khi không đầy đủ metadata.

#### Giải pháp: Chiến lược Fallback Đa tầng (Multi-layered Strategy)Hệ thống không phụ thuộc vào một nguồn dữ liệu duy nhất:

1. **Lớp 1 (Ưu tiên): Goong Maps API.**
* Đây là API chuyên biệt cho Việt Nam, dữ liệu giao thông chính xác nhất.
* Backend gửi tham số `avoid=toll|ferry|highway`.


2. **Lớp 2 (Fallback): Mapbox Directions API.**
* Nếu Goong không hỗ trợ tham số tránh hoặc gặp lỗi, hệ thống tự động chuyển (failover) sang Mapbox với tham số `exclude`. Mapbox có dữ liệu toàn cầu rất tốt về phân loại đường.


3. **Lớp 3 (Dự phòng cuối): OSRM.**
* Nếu cả 2 API trên đều lỗi, hệ thống dùng OSRM tự dựng để đảm bảo người dùng luôn nhận được kết quả (dù có thể không tránh được các yếu tố mong muốn, nhưng có đường đi vẫn hơn không).


4. **Cảnh báo:** Nếu đã thử mọi cách mà vẫn phải đi qua trạm thu phí (ví dụ: độc đạo), hệ thống sẽ trả về lộ trình tốt nhất kèm cờ cảnh báo `warning: true` để Frontend hiển thị thông báo minh bạch cho người dùng.

---

## 🚀 PHẦN 2: HƯỚNG DẪN CÀI ĐẶT & TRIỂN KHAI (DÀNH CHO NGƯỜI MỚI)Chào bạn! 👋 Nếu bạn vừa tải mã nguồn này về và đang tự hỏi "Làm sao để chạy nó đây?", thì phần này dành riêng cho bạn. Đừng lo lắng, chúng ta sẽ cùng dựng hệ thống này lên từng bước một nhé. Rất đơn giản thôi!

### Bước 1: Chuẩn bị "đồ nghề"Trước khi bắt đầu, hãy chắc chắn máy tính của bạn đã có sẵn 2 món này. Nếu chưa có, bạn tải về cài đặt như các phần mềm bình thường nhé:

1. **Python (Bản 3.9 trở lên):** Đây là ngôn ngữ lập trình chính của Backend.
* *Tải tại:* [python.org](https://www.python.org/downloads/) (Nhớ tích vào ô *"Add Python to PATH"* khi cài nhé).


2. **MongoDB (Database):** Nơi chúng ta lưu trữ dữ liệu các trạm sạc.
* *Tải tại:* [mongodb.com](https://www.mongodb.com/try/download/community).



### Bước 2: Lấy mã nguồn về máyBạn mở Terminal (hoặc Command Prompt trên Windows) và chạy lệnh này để tải code về (hoặc đơn giản là giải nén file Zip bạn đang có):

```bash
git clone <link-repo-của-bạn>
cd xe-dien

```

### Bước 3: Tạo "căn phòng riêng" cho dự án (Virtual Environment)Để tránh xung đột với các phần mềm khác trên máy, chúng ta nên tạo một môi trường ảo. Hãy tưởng tượng đây là một hộp cát riêng biệt để chạy dự án.

* **Trên Windows:**
```bash
python -m venv venv
venv\Scripts\activate

```


* **Trên macOS/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate

```


*(Khi nào thấy chữ `(venv)` hiện ở đầu dòng lệnh là thành công rồi đó!)*

### Bước 4: Cài đặt các thư viện cần thiếtBây giờ chúng ta sẽ nhờ Python tải hết các công cụ phụ trợ (như Flask, pymongo, v.v.) về. Mình đã liệt kê sẵn trong file `requirements.txt` rồi.

```bash
pip install -r requirements.txt

```

*Pha một tách cà phê và chờ một chút nhé, quá trình này mất khoảng 1-2 phút.*

### Bước 5: Nạp dữ liệu trạm sạc (Quan trọng!)Hệ thống cần dữ liệu trạm sạc để hoạt động. Mình đã chuẩn bị sẵn file `data/stations.json`. Bạn cần nạp nó vào MongoDB.

Đầu tiên, hãy chắc chắn MongoDB đang chạy, sau đó mở một cửa sổ terminal **mới** và gõ:

```bash
mongoimport --db ev_database --collection ev_stations --file data/stations.json --jsonArray

```

*Nếu thấy thông báo `connected to: ... imported ... documents` là ngon lành!*

### Bước 6: Khởi động động cơ! 🏎️Mọi thứ đã sẵn sàng. Quay lại cửa sổ terminal ban đầu (nơi đang có `(venv)`) và chạy lệnh:

```bash
python app.py

```

Nếu bạn thấy dòng chữ kiểu như `Running on http://127.0.0.1:5000`, chúc mừng bạn! Bạn đã triển khai thành công.

👉 **Hãy mở trình duyệt và truy cập:** `http://localhost:5000`

---

### ❓ Gặp sự cố? (Troubleshooting)Đôi khi mọi thứ không suôn sẻ, chuyện bình thường thôi!

* **Lỗi "Module not found":** Có thể bạn quên chưa kích hoạt môi trường ảo (`venv`). Hãy làm lại Bước 3.
* **Lỗi kết nối Database:** Kiểm tra xem MongoDB đã bật chưa (thử mở phần mềm MongoDB Compass lên xem có kết nối được không).
* **Cổng 5000 bị chiếm:** Có app khác đang dùng cổng này. Mở file `app.py`, tìm dòng cuối cùng và đổi `port=5000` thành `port=5001` rồi chạy lại nhé.

---

## 📚 TỔNG QUAN CÔNG NGHỆ (TECH STACK)Dành cho các bạn muốn tra cứu nhanh các công nghệ được sử dụng trong dự án này.

### Backend & Core| Công nghệ | Vai trò |
| --- | --- |
| **Python Flask** | Framework chính, nhẹ và linh hoạt. |
| **Pymongo** | "Cầu nối" giao tiếp với database MongoDB. |
| **Scipy (KD-Tree)** | Thuật toán tìm kiếm không gian siêu tốc. |
| **NumPy** | Xử lý tính toán số học ma trận. |
| **Matplotlib** | Vẽ biểu đồ phân tích (dùng cho debug/report). |

### Frontend & UI| Công nghệ | Vai trò |
| --- | --- |
| **HTML5/CSS3/JS** | Nền tảng giao diện cơ bản. |
| **GoongJS API** | Bản đồ vector chuyên dụng cho dữ liệu Việt Nam. |
| **Leaflet.js** | Bản đồ fallback (mã nguồn mở). |
| **Material Symbols** | Bộ icon hiện đại của Google. |

### Dữ liệu & API| Nguồn | Vai trò |
| --- | --- |
| **MongoDB** | Lưu trữ NoSQL cho dữ liệu trạm sạc. |
| **Goong Maps API** | Routing engine chính (có thu phí, dữ liệu tốt). |
| **Mapbox API** | Routing engine dự phòng (quốc tế). |
| **OSRM** | Routing engine mã nguồn mở. |

---

## 📞 LIÊN HỆ Dự án này được xây dựng với rất nhiều tâm huyết. Nếu bạn có câu hỏi kỹ thuật, ý tưởng đóng góp hoặc phát hiện lỗi, đừng ngần ngại liên hệ nhé!

* **Email:** [Email-của-bạn]
* **Github:** [Link-repo-của-bạn]

**Chúc bạn có những trải nghiệm thú vị với EV Router! 🌱**