# FloodFight69 · กทม. 2569

Dashboard ติดตามสถานการณ์น้ำท่วมกรุงเทพฯ: ประกาศเตือนภัยกรมอุตุฯ, ประกาศ ปภ./ศูนย์พักพิง, เรดาร์ฝนสุวรรณภูมิ, แผนที่ถนนน้ำท่วมที่ กทม. แนะให้เลี่ยง, ระดับน้ำ ThaiWater, อัตราการไหลเจ้าพระยา (GloFAS), พยากรณ์ 48 ชม./7 วัน และฟีดข่าว

อัปเดตอัตโนมัติทุก 30 นาทีด้วย GitHub Actions (`.github/workflows/refresh.yml`) แล้วเผยแพร่ผ่าน GitHub Pages

## ไฟล์
- `fetch_feed.py` ดึงข้อมูลทุกแหล่ง เขียน `data/feed.json` และ `data/radar/f*.json`
- `template.html` หน้าเว็บ (ใช้ได้ทั้งบน GitHub Pages และ Claude Artifact)
- `build_site.py` สร้าง `index.html` จาก template + data
- `data/alerts.json` ประกาศ ปภ. ที่เพิ่มด้วยมือ (แก้ไฟล์นี้เพื่อเพิ่มประกาศ)

## รันเอง
```
pip install pillow
python fetch_feed.py data/feed.json   # RADAR_DIR=data/radar GEO_CACHE=data/geocache.json
python build_site.py
```

## แหล่งข้อมูล
Open-Meteo (พยากรณ์, GloFAS), กรมอุตุนิยมวิทยา (TMD Open Data, เรดาร์สุวรรณภูมิ), ThaiWater / สสน., Google News RSS, ไทยรัฐ (รายการถนนจากระบบเตือนน้ำท่วมถนน กทม.), Nominatim, แผนที่ © OpenStreetMap contributors

ข้อมูลใช้ประกอบการตัดสินใจเท่านั้น ตรวจสอบประกาศทางการเสมอ · สายด่วน กทม. 1555 · ปภ. 1784
