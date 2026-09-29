# FloodFight69 · กทม. 2569

Dashboard ติดตามสถานการณ์น้ำท่วมกรุงเทพฯ: ประกาศเตือนภัยกรมอุตุฯ, ประกาศ ปภ./ศูนย์พักพิง, เรดาร์ฝนสุวรรณภูมิ, แผนที่ถนนน้ำท่วมที่ กทม. แนะให้เลี่ยง, ระดับน้ำบนถนนสดจากเซนเซอร์ กทม. 251 จุด, รายงานน้ำท่วมถนนสดจาก จส.100 และ สวพ.91, ระดับน้ำ ThaiWater, อัตราการไหลเจ้าพระยา (GloFAS), พยากรณ์ 48 ชม./7 วัน และฟีดข่าว

แท็บ **ระยอง**: ประกาศเตือนที่กล่าวถึง จ.ระยอง, ฝนที่วัดได้จริงจากสถานีโทรมาตร, ฝนพยากรณ์รายอำเภอ, ระดับน้ำ, อ่างเก็บน้ำขนาดใหญ่ (ประแสร์, หนองปลาไหล), พยากรณ์ภาคตะวันออก และข่าว

อัปเดตอัตโนมัติทุก 1 ชั่วโมงด้วย GitHub Actions (`.github/workflows/refresh.yml`) แล้วเผยแพร่ผ่าน GitHub Pages

## ดูข้อมูลผ่านเว็บ

เว็บไซต์
```
https://lazarussp1.github.io/FloodFight69/
```

## ไฟล์
- `fetch_feed.py` ดึงข้อมูลทุกแหล่ง เขียน `data/feed.json` และ `data/radar/f*.json`
- `template.html` หน้าเว็บ (ใช้ได้ทั้งบน GitHub Pages และ Claude Artifact)
- `build_site.py` สร้าง `index.html` จาก template + data
- `data/alerts.json` ประกาศ ปภ. ที่เพิ่มด้วยมือ (แก้ไฟล์นี้เพื่อเพิ่มประกาศ)
- `bma_push.py` ส่งข้อมูลเซนเซอร์น้ำบนถนนของ กทม. ขึ้น `data/bma.json` จากเครื่องที่เว็บ กทม. ยอมให้เข้า (ดูหัวข้อถัดไป)
- `Dockerfile.bma`, `compose.yml`, `.env.example` รัน `bma_push.py` ทุกชั่วโมงใน Docker

## เซนเซอร์น้ำบนถนน กทม.
เว็บ weather.bangkok.go.th ใช้ Cloudflare ปิดกั้นเซิร์ฟเวอร์ของ GitHub Actions บอทจึงดึงข้อมูลเองไม่ได้ (เหตุผลบันทึกไว้ใน `bma_err` ของ `data/feed.json`) จึงใช้เครื่องที่เปิดตลอดและใช้อินเทอร์เน็ตบ้าน/ออฟฟิศ (ไม่ใช่ cloud server) ส่งข้อมูลขึ้นมาแทน

ติดตั้งด้วย Docker บนเครื่องนั้น:
```
git clone https://github.com/LazarusSP1/FloodFight69.git && cd FloodFight69
cp .env.example .env            # ใส่ GH_TOKEN (ดูวิธีสร้าง token ในไฟล์)
docker compose run --rm bma-relay python bma_push.py --dry-run   # ทดสอบ: ต้องได้ "fetched ... sites"
docker compose up -d --build     # รันทุกชั่วโมง เริ่มเองหลังรีบูต · ดู log: docker compose logs -f
```
เมื่อโค้ดใน repo เปลี่ยน ให้ `git pull && docker compose up -d --build` · รันตรงโดยไม่ใช้ Docker ก็ได้: `python bma_push.py` (ใช้ `GH_TOKEN` หรือ gh CLI ที่ล็อกอินไว้)

บอทจะใช้ `data/bma.json` เมื่ออายุไม่เกิน 6 ชม. ถ้าไม่มีข้อมูลที่ใหม่พอ หน้าเว็บจะแสดงลิงก์ไปหน้าสดของสำนักการระบายน้ำแทน

## รันเอง
```
pip install pillow
python fetch_feed.py data/feed.json   # RADAR_DIR=data/radar GEO_CACHE=data/geocache.json
python build_site.py
```

## แหล่งข้อมูล
Open-Meteo (พยากรณ์, GloFAS), กรมอุตุนิยมวิทยา (TMD Open Data, เรดาร์สุวรรณภูมิ), ThaiWater / สสน. (ระดับน้ำ, ฝนโทรมาตร, อ่างเก็บน้ำของกรมชลประทาน), สำนักการระบายน้ำ กทม. (เซนเซอร์น้ำบนถนน weather.bangkok.go.th/flood/), Google News RSS, ไทยรัฐ (รายการถนนจากระบบเตือนน้ำท่วมถนน กทม.), จส.100 (js100.com), สวพ.91 (fm91bkk.com), Nominatim, แผนที่ © OpenStreetMap contributors

ข้อมูลใช้ประกอบการตัดสินใจเท่านั้น ตรวจสอบประกาศทางการเสมอ · สายด่วน กทม. 1555 · ปภ. 1784
