const { MongoClient } = require("mongodb");
const fs = require("fs");
require("dotenv").config();

const uri = process.env.MONGO_URI;
const client = new MongoClient(uri);

async function importData() {
  await client.connect();
  const db = client.db("ev_database");
  const collection = db.collection("ev_stations");

  const data = JSON.parse(fs.readFileSync("ev_stations.json", "utf8"));
  await collection.insertMany(data);

  console.log(`Đã import ${data.length} trạm sạc thành công!`);
  await client.close();
}

importData();
