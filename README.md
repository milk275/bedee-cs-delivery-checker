# BeDee CS Delivery Checker

ระบบภายในสำหรับทีม CS ใช้ค้นหาเลขออเดอร์หรือเลข Track จากหน้าเดียว รองรับ
KLEAN&KARE (Skyfrog), KEX และ InterExpress รวมถึงออเดอร์ที่มีหลาย Track

Repository นี้เป็น **CS-only**:

- หน้าออเดอร์ รายงาน CSV และรูปหลักฐานในหน้า CS ต้องผ่าน PIN
- PIN เก็บเป็น password hash (`CS_ACCESS_PIN_HASH`) ไม่เก็บ PIN แบบ plain text
- รหัสผู้ให้บริการและ Supabase key อยู่ใน `.env` ฝั่งเซิร์ฟเวอร์เท่านั้น
- สถานะแคชและ Mapping อยู่บน Supabase ไม่มี SQLite
- ผล Skyfrog จะตัดรูปที่ 1–2 ซึ่งเป็นลายเซ็นออกก่อนแสดง
- งานตรวจสถานะอัตโนมัติรันรายชั่วโมง 08:00–22:00 (เวลาไทย)
- หน้า `/admin` ตรวจการเชื่อมต่อแบบ Real-time และเวลารอบล่าสุด โดยไม่แสดง secret

## เริ่มใช้งานด้วย Docker Compose

ต้องมี Docker Engine และ Docker Compose v2

```bash
cp .env.example .env
```

สร้าง `WEB_SECRET_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

สร้าง hash ของ PIN โดยไม่บันทึก PIN ลงใน source code:

```bash
docker run --rm python:3.12-slim sh -c \
  "pip install 'Werkzeug>=3.1,<4' >/dev/null && python -c \"from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass('PIN: ')))\""
```

นำค่าที่ได้ใส่ `WEB_SECRET_KEY` และ `CS_ACCESS_PIN_HASH` ใน `.env` จากนั้นกรอก
credentials ของขนส่ง, Google Sheet และ Supabase ให้ครบ

สร้างตาราง Supabase โดยรันไฟล์ [`supabase/schema.sql`](supabase/schema.sql)
ใน Supabase SQL Editor หนึ่งครั้ง แล้วเริ่มระบบ:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

เข้าใช้งานที่ `http://127.0.0.1:8091/login` หรือโดเมน HTTPS ที่ผูกผ่าน
Cloudflare Tunnel

หลังเข้าสู่ระบบ เปิด `/admin` เพื่อตรวจ Skyfrog, KEX, InterExpress, Google Sheet,
Apps Script, Supabase, รายงานรายชั่วโมง และ Shopee Bot → Supabase หน้าเว็บจะ
แยกเวลา “ดาวน์โหลด Shopee ล่าสุด” ออกจาก “นำเข้า Supabase ล่าสุด” เพื่อไม่ให้
การนำไฟล์เก่าซ้ำแสดงเป็นสถานะปกติ

หาก Shopee automation รันอยู่นอก Compose ให้ตั้งโฟลเดอร์รายงานบนเครื่อง host:

```dotenv
SHOPEE_REPORT_HOST_PATH=/home/your-user/kleanandkare-shopee/sales-reports
```

## เปิดเข้าสู่ระบบ Shopee เมื่อ session หมดอายุ

เมื่อบอทตรวจพบว่า Shopee session หมดอายุ หน้าแรกจะแสดงปุ่ม
**เปิดหน้าเข้าสู่ระบบ Shopee** ให้เจ้าหน้าที่เปิด Chromium ของโปรเจกต์นี้ผ่าน
noVNC และกรอก OTP ได้เอง โดยไม่ต้องเปิด VNC สู่ Internet สาธารณะ
เมื่อเข้าสู่ระบบใหม่แล้วบอทตรวจสอบสำเร็จ ระบบจะบันทึกเวลาเข้าสู่ระบบล่าสุดและ
แสดงวันที่คาดว่าจะต้องยืนยัน OTP รอบถัดไปอีก 7 วันบนการ์ด Shopee
เจ้าหน้าที่กด **เข้าสู่ระบบแล้ว ตรวจสอบทันที** เพื่อให้ Pi ปิด VNC และรันบอท
ยืนยัน session ใหม่ได้ทันที โดยไม่ต้องรอรอบรายชั่วโมง

บน Raspberry Pi ให้ตั้งค่า path ที่ container ใช้ส่งคำขอ และ URL noVNC ที่ผูกกับ
Tailscale IP ของ Pi:

```dotenv
SHOPEE_CONTROL_HOST_PATH=/home/your-user/bedee-cs-control
SHOPEE_VNC_URL=http://100.x.x.x:6081/vnc.html?autoconnect=1&resize=scale&reconnect=1
SHOPEE_VNC_WINDOW_MINUTES=20
```

สร้าง control directory ให้ UID/GID `10001` ของ container เขียนได้ แล้วติดตั้ง
ตัวเฝ้ารอคำขอบน host:

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /home/your-user/bedee-cs-control
sudo install -m 0644 deploy/systemd/bedee-shopee-login.path /etc/systemd/system/
sudo install -m 0644 deploy/systemd/bedee-shopee-login.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/bedee-shopee-login-stop.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/bedee-shopee-login-stop.timer /etc/systemd/system/
sudo install -m 0644 deploy/systemd/bedee-shopee-verify.path /etc/systemd/system/
sudo install -m 0644 deploy/systemd/bedee-shopee-verify.service /etc/systemd/system/
sudo install -d -m 0755 /etc/systemd/system/kleanandkare-shopee-report.service.d
sudo install -m 0644 \
  deploy/systemd/kleanandkare-shopee-report.service.d/bedee-login.conf \
  /etc/systemd/system/kleanandkare-shopee-report.service.d/
sudo install -d -m 0755 /etc/systemd/system/kleanandkare-shopee-vnc.service.d
sudo install -m 0644 \
  deploy/systemd/kleanandkare-shopee-vnc.service.d/bedee-login.conf \
  /etc/systemd/system/kleanandkare-shopee-vnc.service.d/
sudo systemctl daemon-reload
sudo systemctl enable --now bedee-shopee-login.path
sudo systemctl enable --now bedee-shopee-verify.path
```

เครื่องของเจ้าหน้าที่ต้องเชื่อมต่อ Tailscale network เดียวกับ Pi ก่อนเปิดปุ่ม
ระบบจะปิด noVNC และ browser session อัตโนมัติหลัง 20 นาที ปุ่มนี้อยู่หลังหน้า
PIN, จำกัดความถี่ และส่งได้เพียงคำสั่งเปิดหน้าล็อกอินที่กำหนดไว้เท่านั้น

> หากทดสอบผ่าน HTTP ในเครื่อง ให้ตั้ง `WEB_COOKIE_SECURE=false` ชั่วคราว
> แต่ production ที่เป็น HTTPS ต้องใช้ `true`

## Services ใน Compose

- `web` — หน้า CS และ API ที่ต้องผ่าน PIN
- `status-scheduler` — ตรวจสถานะจาก Google Sheet ทุกชั่วโมง 08:00–22:00
- `shopee-sync` — โปรไฟล์เสริมสำหรับดาวน์โหลด Shopee Sell Report และอัปเดต
  Mapping (ไม่เริ่มโดยอัตโนมัติ)

เปิด Shopee automation เมื่อเตรียม browser session แล้ว:

```bash
docker compose --profile shopee up -d --build
```


สำหรับเครื่อง RAM น้อย แนะนำให้รันเฉพาะ `web` และ `status-scheduler` ก่อน เพราะ
Chromium ใน `shopee-sync` ใช้หน่วยความจำมากกว่า service อื่น

## คำสั่งดูแลระบบ

```bash
# ดูสถานะ
docker compose ps

# ดู log
docker compose logs -f --tail=200 web status-scheduler

# อัปเดตเวอร์ชันใหม่
git pull
docker compose up -d --build

# หยุดระบบ (ข้อมูลใน named volumes ยังอยู่)
docker compose down

# สำรองชื่อและตำแหน่ง volumes
docker volume ls | grep bedee-cs-delivery-checker
```

## ความปลอดภัย

- ห้าม commit `.env`, browser session, รายงาน, รูป POD หรือไฟล์ฐานข้อมูล
- ใช้ Private GitHub repository และจำกัดสมาชิกที่เข้าถึง
- ใช้ Supabase secret/service-role key เฉพาะฝั่ง server ห้ามส่งให้ browser
- ตาราง Supabase เปิด RLS และอนุญาตเฉพาะ `service_role`
- URL รูปสำหรับ Google Sheets ใช้ HMAC bearer token และรับเฉพาะแหล่งรูปที่ระบบ
  อนุญาต จึงเปิดได้โดย Google Sheets โดยไม่เปิดหน้ารายงาน CS
- เปลี่ยน PIN และรหัสขนส่งเป็นระยะ แล้ว restart ด้วย
  `docker compose up -d --force-recreate`
- เปิด Cloudflare Access เพิ่มอีกชั้นสำหรับโดเมน CS หากต้องการจำกัดอีเมลพนักงาน

## ทดสอบ

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e . pytest
pytest
```

Docker image เป็น multi-architecture และใช้ได้ทั้ง Raspberry Pi (`arm64`) และ
DigitalOcean Droplet (`amd64`)
