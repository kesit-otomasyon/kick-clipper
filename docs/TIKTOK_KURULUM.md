# TikTok kurulumu

Kesitler YouTube'a gittiği gibi TikTok'a da gidecek. Bunun için TikTok'ta bir "uygulama" kaydı gerekiyor.

## Hazır olanlar (Claude yaptı)
- Site sayfaları (GitHub Pages'te yayınlanacak):
  - Ana sayfa: https://kesit-otomasyon.github.io/kick-clipper/
  - Gizlilik: https://kesit-otomasyon.github.io/kick-clipper/privacy.html
  - Şartlar: https://kesit-otomasyon.github.io/kick-clipper/terms.html
  - Giriş dönüş sayfası: https://kesit-otomasyon.github.io/kick-clipper/callback.html
- Uygulama simgesi (1024x1024): `docs/tiktok_icon.png`

## Senin adımların

**1. GitHub Pages'i aç (1 dk)**
- github.com/kesit-otomasyon/kick-clipper → Settings → Pages.
- Source: "Deploy from a branch". Branch: `main`, klasör: `/docs` → Save.
- 1-2 dakika sonra yukarıdaki ana sayfa açılmalı.

**2. TikTok hesabı**
- Kesitlerin yükleneceği TikTok hesabını aç (örneğin "Efsane Yayın Anları"). Telefonda girişli kalsın.

**3. Geliştirici hesabı**
- developers.tiktok.com → Log in. Kesit hesabıyla ya da kendi e-postanla girebilirsin.
- Sağ üstteki profil simgesi → "Manage apps" → "Connect an app".
- Sahip olarak kendi bireysel hesabını seç.

**4. Uygulama bilgileri**
- App name: `Efsane Yayin Anlari Uploader`. Adında "TikTok" geçmesin.
- Icon: `docs/tiktok_icon.png`
- Category: Entertainment
- Description: `Posts short clips of Turkish Kick live streams (with the streamers' permission) to our own TikTok account.`
- Terms of Service URL ve Privacy Policy URL: yukarıdaki şartlar ve gizlilik adresleri.
- Platforms: Web → Website URL: ana sayfa adresi.

**5. Ürünler**
- "Add products" → **Login Kit** ve **Content Posting API**.
- Login Kit → Redirect URI: callback adresi.
- Content Posting API → "Direct Post"u aç.
- İzinler (scopes): `user.info.basic`, `video.upload`, `video.publish`.

**6. Adres doğrulama**
- "URL properties" / "Verify" → "URL prefix" seç, adres olarak `https://kesit-otomasyon.github.io/kick-clipper/` gir.
- TikTok'un verdiği doğrulama dosyasını indir ve Claude'a söyle. Claude dosyayı siteye koyar, sen "Verify"a basarsın.

**7. Sandbox (incelemeden önce deneme)**
- Uygulama sayfasında "Sandbox" → yeni sandbox oluştur → "Target users" kısmına kesit TikTok hesabını ekle.

**8. Anahtarlar**
- Sandbox sayfasındaki **Client key** ve **Client secret**'ı kimseye yapıştırma, sohbete de yazma.
- GitHub'da Settings → Secrets → Actions → `TIKTOK_CLIENT_KEY` ve `TIKTOK_CLIENT_SECRET` olarak ekle.
- Aynısını bilgisayarda `C:\Users\kemal\kick-clipper\credentials\tiktok_app.json` dosyasına yaz: `{"client_key": "...", "client_secret": "..."}`. Bu dosya git'e gitmez.

**9. Hesabı bağlama**
- Claude bir giriş bağlantısı açar. Kesit hesabıyla girip izin verirsin. Claude dönüş kodunu alıp bilgisayarda bir anahtar (token) dosyası yapar.
- Bu dosyanın içeriğini GitHub'a `TIKTOK_TOKEN` secret'ı olarak sen eklersin.

**10. Deneme ve inceleme**
- Sandbox'ta sistem kesitleri "sadece bana görünür" ya da taslak olarak yükler. Kontrol edersin.
- Sonra uygulamayı "Submit for review" ile gönderirsin. Claude ekran kaydı ve açıklama metinlerini hazırlar.
- Onay gelince kesitler herkese açık ve tamamen otomatik paylaşılır.

## Bilinmesi gerekenler
- Onaydan önce TikTok'a giden videolar herkese açık olamaz.
- Anahtar 2 saatte bir, sistem tarafından kendiliğinden yenilenir. Yenileme hakkı 180 gün geçerli. Her yenilemede yeni bir anahtar gelir, sistem onu saklar.
- eray ve rraenee'nin yazılı izni (örneğin bir mesaj ekran görüntüsü) inceleme sırasında işe yarar.
