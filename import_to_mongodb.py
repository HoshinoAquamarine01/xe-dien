import json
from pymongo import MongoClient

# Kết nối MongoDB
client = MongoClient('mongodb://localhost:27017/')
db = client['ev_database']
collection = db['ev_stations']

# Đọc và import JSON
with open('ev_stations.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
    collection.insert_many(data)

print(f"Da import {len(data)} tram sac thanh cong!")
