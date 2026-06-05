# World Cup 2026 → Google Calendar (tự cập nhật)

Lịch đầy đủ **104 trận** FIFA World Cup 2026, sinh ra file `worldcup.ics`, host công khai
trên GitHub. GitHub Actions **tự crawl lại mỗi sáng (05:00 giờ VN)** và cập nhật file —
khi vòng knock-out có đội thật, các trận "2A / W74 / 3A/B/C/D/F…" sẽ tự được điền tên đội.

- Giờ hiển thị: theo múi giờ máy bạn (file lưu chuẩn UTC; Google tự đổi sang giờ VN).
- Mỗi trận có sẵn nhắc nhở **trước 60 phút**.
- Nguồn dữ liệu: [openfootball/worldcup](https://github.com/openfootball/worldcup).

---

## Cài đặt (làm 1 lần)

### 1. Đẩy code lên repo
Trong thư mục này:
```bash
git init -b main          # nếu chưa có
git remote add origin https://github.com/nh0znoisung/wc2026-calendar.git
git add .
git commit -m "World Cup 2026 auto calendar"
git push -u origin main
```
> Repo phải để **Public** thì Google mới đọc được link .ics.

### 2. Bật quyền ghi cho Actions
Trên GitHub: **Settings → Actions → General → Workflow permissions** →
chọn **Read and write permissions** → Save.
(Cần để job tự commit file `worldcup.ics` đã cập nhật.)

### 3. Chạy thử workflow
Tab **Actions** → chọn *Update World Cup 2026 calendar* → **Run workflow**.
Sau ~1 phút, file `worldcup.ics` sẽ xuất hiện/được cập nhật trong repo.

### 4. Subscribe vào Google Calendar
Mở Google Calendar (bản web) → bên trái, cạnh **"Other calendars"** bấm **+** →
**From URL** → dán link này:

```
https://raw.githubusercontent.com/nh0znoisung/wc2026-calendar/main/worldcup.ics
```

→ **Add calendar**. Lịch hiện thành **một calendar riêng có checkbox bật/tắt**.
Không cần token hay đăng nhập gì thêm.

---

## Cách nó tự cập nhật

- **GitHub Actions** (file `.github/workflows/update.yml`) chạy theo cron `0 22 * * *`
  (22:00 UTC = 05:00 VN) — clone dữ liệu mới, sinh lại `worldcup.ics`, commit nếu có thay đổi.
  Chạy trên cloud GitHub nên **máy bạn tắt vẫn chạy**.
- **Google Calendar** tự đọc lại link subscribe định kỳ (~mỗi 8–24h, do Google quyết định,
  không chỉnh tần suất được). Vì mỗi trận có UID cố định nên cập nhật **đè tại chỗ**, không trùng.

Muốn đổi giờ chạy: sửa dòng `cron` trong `update.yml` (theo giờ UTC).

---

## Chạy tay ở máy (tùy chọn)
```bash
pip install icalendar
git clone --depth 1 https://github.com/openfootball/worldcup.git _data
python generate_ics.py --data-dir "$(echo _data/2026--*)" --out worldcup.ics
```

## Files
| File | Vai trò |
|---|---|
| `generate_ics.py` | Parse dữ liệu → sinh `worldcup.ics` |
| `worldcup.ics` | File lịch (cái Google subscribe) |
| `.github/workflows/update.yml` | Cron tự cập nhật mỗi sáng |
