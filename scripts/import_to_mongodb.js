const { MongoClient } = require('mongodb');
const fs = require('fs');

const uri = 'mongodb://localhost:27017';
const client = new MongoClient(uri);

async function importData() {
    await client.connect();
    const db = client.db('ev_database');
    const collection = db.collection('ev_stations');
    
    const data = JSON.parse(fs.readFileSync('ev_stations.json', 'utf8'));
    await collection.insertMany(data);
    
    console.log(`Đã import ${data.length} trạm sạc thành công!`);
    await client.close();
}

importData();
