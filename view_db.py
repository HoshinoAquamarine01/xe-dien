from flask import Flask, jsonify, render_template_string
from pymongo import MongoClient

app = Flask(__name__)

HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>MongoDB Viewer</title>
    <style>
        body { font-family: Arial; padding: 20px; background: #f5f5f5; }
        h1 { color: #2c3e50; }
        .stats { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .station { background: white; padding: 15px; margin: 10px 0; border-radius: 8px; border-left: 4px solid #3498db; }
        .station h3 { margin: 0 0 10px 0; color: #2980b9; }
        .info { color: #7f8c8d; font-size: 14px; }
        button { background: #3498db; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #2980b9; }
        #stations { margin-top: 20px; }
    </style>
</head>
<body>
    <h1>🗄️ MongoDB Database Viewer</h1>
    <div class="stats">
        <h2>Database: ev_database</h2>
        <p>Collection: ev_stations</p>
        <p id="count">Loading...</p>
    </div>
    <button onclick="loadStations()">Xem tất cả trạm sạc</button>
    <button onclick="loadSample()">Xem 20 trạm mẫu</button>
    <div id="stations"></div>

    <script>
        fetch('/count').then(r => r.json()).then(data => {
            document.getElementById('count').innerHTML = `<strong>Tổng số trạm: ${data.count}</strong>`;
        });

        function loadStations() {
            document.getElementById('stations').innerHTML = '<p>Đang tải...</p>';
            fetch('/stations').then(r => r.json()).then(data => {
                displayStations(data.stations);
            });
        }

        function loadSample() {
            document.getElementById('stations').innerHTML = '<p>Đang tải...</p>';
            fetch('/stations?limit=20').then(r => r.json()).then(data => {
                displayStations(data.stations);
            });
        }

        function displayStations(stations) {
            let html = `<h2>Hiển thị ${stations.length} trạm</h2>`;
            stations.forEach((s, i) => {
                html += `
                    <div class="station">
                        <h3>${i+1}. ${s.name}</h3>
                        <div class="info">
                            📍 ${s.address || 'N/A'}<br>
                            📊 Loại: ${s.type || 'DC Fast'} | Công suất: ${s.power || 150} kW<br>
                            🌍 Tọa độ: ${s.lat}, ${s.lon}
                        </div>
                    </div>
                `;
            });
            document.getElementById('stations').innerHTML = html;
        }
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/count')
def count():
    client = MongoClient('mongodb://localhost:27017/')
    db = client['ev_database']
    count = db['ev_stations'].count_documents({})
    client.close()
    return jsonify({'count': count})

@app.route('/stations')
def stations():
    from flask import request
    limit = int(request.args.get('limit', 0))
    
    client = MongoClient('mongodb://localhost:27017/')
    db = client['ev_database']
    
    if limit > 0:
        data = list(db['ev_stations'].find({}, {'_id': 0}).limit(limit))
    else:
        data = list(db['ev_stations'].find({}, {'_id': 0}))
    
    client.close()
    return jsonify({'stations': data})

if __name__ == '__main__':
    print("Mo trinh duyet va truy cap: http://localhost:5001")
    app.run(debug=True, port=5001)
