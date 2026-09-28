# Sistem Performansı

Windows 10 Görev Yöneticisi'nin Performans sayfasından esinlenen, Linux masaüstü için canlı sistem izleyicisi.

## Başlatma

`Sistem Performansı.desktop` dosyasına çift tıklayın. Uygulama tek örnek çalışır; yeniden başlatma var olan pencereyi öne getirir. Sol panelin sağındaki ayırıcıyı sürükleyerek menü genişliğini değiştirebilirsiniz. Pencereyi kapatmak uygulamayı sistem tepsisine küçültür. Tepsi simgesine sağ tıklayıp **Sistemle birlikte başlat** seçeneğini işaretleyerek oturum açılışında çalışmasını açıp kapatabilir, **Tamamen kapat** ile uygulamayı sonlandırabilirsiniz. Tepsi simgesine sol tıklamak pencereyi geri getirir.

Güncelleme aralığı başlık çubuğundan **1, 2, 3, 5 veya 10 saniye** olarak seçilir ve sonraki açılışta da korunur.

## Görüntülenen veriler

- **CPU:** toplam ve mantıksal işlemci başına kullanım, anlık ortalama frekans, temel/üst hız, çekirdek/soket/iş parçacığı sayıları, sanallaştırma desteği, önbellek, sıcaklık sensörleri, süreç ve Linux dosya tanıtıcısı sayısı, çalışma süresi.
- **Bellek:** RAM toplam/kullanılan/kullanılabilir miktarı ve yüzdesi, swap miktarı ve yüzdesi, commit, önbellek, etkin bellek, arabellekler ve slab.
- **Diskler:** tüm görünür fiziksel blok aygıtları için etkin süre, okuma/yazma MB/s, ortalama I/O yanıt süresi ve toplam sayaçlar; sistem diskini de belirterek aygıtlara bağlı dosya sistemlerinde kullanılan/boş/toplam alan ve doluluk yüzdesi.
- **Ağ:** her fiziksel ağ arayüzü için anlık indirme/yükleme MB/s, toplam veri, bağlantı durumu/hızı, MTU, IP/MAC ve varsa Wi-Fi sinyali/SSID.
- **Bluetooth:** BlueZ tarafından bildirilen adaptör durumu ve bağlı aygıtlar; aygıt sağlıyorsa pil yüzdesi.
- **GPU:** NVIDIA sürücü arayüzünden GPU ve bellek kullanımı, ayrılmış VRAM, sıcaklık, grafik saati, sürücü ve güç verileri; desteklenen AMD/DRM aygıtlarında sysfs sayaçları.

Grafikler son 90 örneği gösterir. Disk ve ağ hızları ondalık MB/s cinsinden verilir; kapasite değerleri GiB tabanlı GB gösterimine çevrilir. GPU paylaşılmış bellek kullanımını veya belirli bir donanım metriğini Linux sürücüsü sunmuyorsa uygulama bunu açıkça "veri yok" olarak gösterir. Bluetooth aygıtlarının görünmesi için sistemde BlueZ ve çalışan Bluetooth servisi gerekir.

## Gereksinimler

- Python 3, GTK 3 Python bağlayıcısı (`PyGObject`) ve `psutil`.
- GPU donanım sayacı için NVIDIA'da `nvidia-smi`; diğer DRM sürücülerinde sunulan `/sys` sayaçları kullanılır.
- Bluetooth için `bluetoothctl` (BlueZ).

Uygulama ek arka plan servisi başlatmaz ve sayaçları Linux `/proc`, `/sys` ve sistem sürücülerinden okur.
