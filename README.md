# CDC V3 + ATR Screener (GitHub Pages)

เว็บสแกนสัญญาณ CDC ActionZone V3 แบบ static ฟรีบน GitHub Pages
GitHub Actions ดึงราคา → คำนวณ → สร้างไฟล์ JSON → deploy หน้าเว็บให้อัตโนมัติ

## ตั้งค่าครั้งแรก

1. สร้าง repo ใหม่บน GitHub (ตั้งเป็น **Public** เพื่อให้ Pages ฟรี) แล้วอัปโหลดไฟล์ทั้งหมดในโฟลเดอร์นี้ (รวมโฟลเดอร์ `.github` และ `docs`)
2. ไปที่ **Settings → Pages → Build and deployment → Source** เลือก **GitHub Actions**
3. ไปที่ **Settings → Actions → General → Workflow permissions** เลือก **Read and write permissions** แล้วกด Save
4. ไปที่แท็บ **Actions → update-and-deploy → Run workflow** (รันครั้งแรกด้วยมือ)
5. รอจนเสร็จ (ประมาณ 1-2 นาที) เว็บจะอยู่ที่ `https://<ชื่อผู้ใช้>.github.io/<ชื่อ repo>/`

## ปรับแต่ง

แก้ไฟล์ `config.json` แล้ว commit ระบบจะรันและ deploy ใหม่เอง
- `symbols` รายชื่อหุ้น (หุ้นไทยใส่ `.BK` เช่น `PTT.BK`, คริปโตใส่ `-USD` เช่น `BTC-USD`)
- `params` พารามิเตอร์ EMA, ATR stop, risk % ฯลฯ (ความหมายเหมือนหน้าเว็บเดิม)

## เวลาอัปเดตอัตโนมัติ (จันทร์-ศุกร์)

| เวลาไทย | เพื่ออะไร |
|---|---|
| 17:30 | หลังตลาดหุ้นไทยปิด |
| 05:30 | หลังตลาดสหรัฐปิด (และคริปโตของวันก่อนหน้า) |

ปรับได้ที่ `cron` ใน `.github/workflows/update.yml` (เวลาใน cron เป็น UTC = เวลาไทย − 7 ชม.)

## แจ้งเตือน Telegram (ไม่บังคับ)

ไปที่ **Settings → Secrets and variables → Actions → New repository secret** เพิ่ม `TG_TOKEN` และ `TG_CHAT_ID`
ระบบจะส่งเมื่อมีสัญญาณใหม่ของแท่งล่าสุด และจำไว้ใน `docs/data/alerts_sent.json` เพื่อไม่ส่งซ้ำ
ถ้าไม่ตั้งค่า ระบบจะข้ามการส่งเงียบๆ

## ทดสอบในเครื่อง

```
pip install -r requirements.txt
python build_static.py --no-alert
python -m http.server 8000 --directory docs     # เปิด http://localhost:8000
```

## ข้อจำกัด

- พารามิเตอร์ปรับสดในหน้าเว็บไม่ได้ ต้องแก้ `config.json` แล้วรอรัน (หรือกด Run workflow)
- ดูกราฟได้เฉพาะหุ้นใน `config.json`
- ข้อมูลจาก Yahoo Finance (ไม่เป็นทางการ) บางครั้งอาจดึงไม่ได้ หุ้นที่ล้มเหลวจะแสดงในแถบสถานะ และข้อมูลเดิมยังอยู่
- หุ้นไทยบน Yahoo อาจล่าช้าหรือขาดบางวัน ควรเทียบกับแอปโบรกเกอร์ก่อนตัดสินใจ
- ผลย้อนหลังไม่ได้รับประกันอนาคต ไม่ใช่คำแนะนำการลงทุน
