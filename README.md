# Bot Kontrol Merkezi

Tek bir **yönetici bottan**, istediğin kadar Telegram botunu yönetirsin: yayın, renkli butonlar, alt menü, karşılama mesajı ve analiz. Hepsi Telegram içinden yapılır.

## Özellikler
- **Sınırsız bot:** Yönetici bota token yapıştırınca bot bağlanır ve çalışır. Restart gerekmez.
- **Yayın:** Tek bota, seçtiğin birkaç bota ya da hepsine gönderebilirsin. Metin, foto, video, GIF, dosya ve **albüm** desteklenir.
- **Buton tıklama takibi:** Her buton için toplam tıklama, tıklayan tekil kişi sayısı ve tıklama oranı görünür. Birden fazla bottan yayın yaptıysan oranı bot bazında da görürsün.
- **Renkli butonlar:** 🟢 yeşil, 🔴 kırmızı, 🔵 mavi.
- **Önizleme, anında gönderim ve zamanlama:** Yayın kullanıcıya nasıl görünecekse önce sen öyle görürsün. İstersen hemen gönderir, istersen saat verirsin (`20:30` veya `12.11 20:30`).
- **Canlı ilerleme:** Bot başına gönderilen, engelleyen ve hata sayıları anlık güncellenir.
- **Yayını geri alma:** Tek tuşla tüm botlardan silinir (Telegram sınırı: 48 saat).
- **Bildirimleri kapat butonu:** Kullanıcı botu engellemek yerine bunu kullanır, spam şikâyeti azalır.
- **Karşılama mesajı:** Her bota ayrı /start mesajı ve butonları.
- **Alt menü:** Sohbetin altında sabit duran butonlar. Her butona ayrı bir yanıt (medya + buton) tanımlanır.
- **Analiz:**
  - Bot başına toplam start, aktif abone, engelleyen ve bildirim kapatan sayıları
  - Bugün / 7 gün / 30 gün yeni abone ve son 7 günde kaybedilen
  - Günlük grafik, dil dağılımı
  - Kaynak takibi (`t.me/botun?start=tgads_kanal1`)
  - Tüm botlarda tekil kişi sayısı
- **Aktiflik kontrolü:** Mesaj göndermeden, botu engelleyenleri ayıklar.
- **CSV:** Abone listesini indirebilirsin.
- **Engelleme takibi:** Biri botu engellediğinde sistem bunu anında görür ve aktif sayısından düşer.

## Kurulum (VPS, tek komut)
1. Telegram'da hazırla: yönetici bot (BotFather), Telegram ID'n (@userinfobot), özel bir depo kanalı.
2. Sunucuya root olarak gir, Claude'un verdiği kurulum komutunu yapıştır. Komut:
   - GitHub için bir okuma anahtarı (deploy key) üretir → GitHub repo → Settings → Deploy keys'e eklersin,
   - Docker, swap, log sınırı, otomatik güncelleme (3 dk'da bir) ve günlük yedeği (04:00) kurar,
   - yönetici bot token'ını ve Telegram ID'ni sorar.
3. Depo kanalına yönetici botu ve diğer botları **admin** ekle, kanala `/depo` yaz. Kanal otomatik ayarlanır.
4. Yönetici bota `/start` → ➕ Bot Ekle.

### Otomatik güncelleme
`main` dalına yeni sürüm gelince sunucu 3 dk içinde kendini günceller ve Telegram'dan haber verir.
Yeni sürüm açılmazsa otomatik eski sürüme döner.

### Yedek
Her gece 04:00'te veritabanı yedeği yönetici bota dosya olarak gelir.
Geri yükleme: dosyayı sunucuya at → `cd /opt/botmerkezi && ./deploy/restore.sh dosya.sql.gz`

## Kullanım ipuçları
- **Buton formatı:**
  ```
  🟢 Hemen Katıl - https://site.com
  🔴 Son Gün! - https://site.com/bonus | 🔵 Kanal - https://t.me/kanal
  ```
  Her satır bir buton sırasıdır, `|` butonları yan yana koyar. Buton istemiyorsan `-` yaz.
- **Renkli butonlar** Şubat 2026 sonrası Telegram sürümlerinde görünür. Eski uygulamalarda butonlar renksiz çıkar ama yine çalışır.
- **Reklam kaynağı takibi:** Telegram Ads'te her kanal için farklı link kullan, örneğin `t.me/botun?start=tgads_kanal1`. Analiz ekranında her kaynaktan kaç kişi geldiği ve kaçının hâlâ aktif olduğu görünür.
- **Albüm:** Telegram albümlere buton eklemeye izin vermiyor. Bu yüzden butonlar albümün hemen altına ayrı bir mesajla gider. O mesajın yazısını buton adımında ilk satıra yazarsın:
  ```
  Yazı: Fırsatı kaçırma 👇
  🟢 Hemen Katıl - https://site.com
  ```
- Gönderim hızı varsayılan olarak bot başına saniyede 25 mesajdır (`SEND_RATE`). Botlar paralel gönderir.
- Bir işlemi yarıda bırakmak için `/iptal`, ana menüye dönmek için `/menu` yaz.

## Buton tıklama takibi (isteğe bağlı)
Takip açıkken butonlardaki linkler kişiye özel bir yönlendirme adresinden geçer, örneğin `https://t.senindomainin.com/r/...`. Kullanıcı hiçbir fark görmez, butona basınca doğrudan hedef siteye gider.
Linkler imzalıdır, yani elle uydurulan tıklamalar sayılmaz.

1. Cloudflare'de bir alt alan adı aç (örnek: `t.senindomainin.com`). VPS IP'ne yönlendir ve proxy'yi (turuncu bulut) açık bırak.
2. VPS'teki nginx'e şu ayarı ekle:
   ```nginx
   server {
       listen 80;
       server_name t.senindomainin.com;
       location / { proxy_pass http://127.0.0.1:8080; }
   }
   ```
   Ardından `nginx -t && systemctl reload nginx` çalıştır.
3. `.env` dosyasında şunları doldur:
   - `PUBLIC_URL=https://t.senindomainin.com`
   - `TRACK_SECRET=` satırına rastgele uzun bir yazı
4. `docker compose up -d` ile yeniden başlat.

Test için `https://t.senindomainin.com/health` adresini aç, `ok` yazısını görmelisin.

`PUBLIC_URL` boş bırakılırsa takip kapalı olur ve butonlar doğrudan hedef linke gider. Ayar yalnızca bundan sonra yapılan yayınlara uygulanır.

## Güncelleme
Yeni sürümü aynı klasöre aç ve `docker compose up -d --build` çalıştır. Veritabanı kendini otomatik günceller, mevcut veriler korunur.

## Yedekleme
```bash
docker compose exec db pg_dump -U botmerkezi botmerkezi > yedek_$(date +%F).sql
```

## Dosya yapısı
```
app/main.py       başlangıç
app/admin.py      yönetici bot menüleri
app/child.py      bağlı botların davranışı (/start, menü, engelleme takibi)
app/manager.py    botları çalıştırma ve durdurma
app/broadcast.py  yayın motoru, zamanlayıcı, silme, aktiflik kontrolü
app/stats.py      analiz
app/keyboards.py  buton ve menü ayrıştırma
app/content.py    tek mesaj / albüm gönderimi, takip linkleri
app/web.py        tıklama takibi yönlendirme sunucusu
app/db.py         veritabanı modelleri
```
