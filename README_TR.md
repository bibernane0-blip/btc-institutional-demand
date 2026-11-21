# BTC Institutional Demand PRO — Ücretsiz sürüm

Bu sürüm ücretli CoinGlass, CryptoQuant veya Render aboneliği gerektirmez.

## Otomatik alınan veriler

- Binance: BTCUSDT fiyat/hacim, Futures Open Interest ve Funding Rate (her 15 dakikada otomatik kontrol).
- Farside Investors: ABD spot Bitcoin ETF günlük toplam net akışı (kaynak güncelledikçe).
- Kote Charts: Bitcoin borsa netflow günlük verisi (ücretsiz API anahtarı gerekir).

**Önemli veri notları**
- ETF ve exchange-netflow günlük veridir; 15 dakikalık görev bunların 15 dakikada bir yayımlandığı anlamına gelmez. Kaynak günlük veriyi yenileyene kadar son güncel kayıt gösterilir.
- Farside verisi açık web tablosundan okunur; site HTML yapısını değiştirirse veri çekimi bozulabilir.
- Kote Charts, etiketlenmiş borsa cüzdanları üzerinden netflow tahmini üretir. Kendi açıklamasında etiketlerin sezgisel/heuristic olduğunu, yanlış veya eksik etiketlerin akışları etkileyebileceğini belirtir. Ani günlük hareketleri tek başına satış sinyali saymayın.
- Kote API anahtarı ücretsizdir; kullanım ticari olmayan kişisel kullanım ve kaynak gösterme koşuluna tabidir. Ürün/hizmet olarak satmak veya ticari amaçla kullanmak için lisans gerekir.
- Üç ana veri kaynağından biri yoksa skor üretilmez. Eksik veri sıfır sayılmaz.

## Skor

Skor -100 ile +100 arasındadır. Ağırlıklar: ETF 20, Exchange Netflow 15, OI 20, Funding 15, fiyat/hacim 30. Bu ağırlıklar deneysel başlangıç varsayımlarıdır, geriye dönük test edilmiş başarı oranı değildir.

## Windows + web arayüzü üzerinden kurulum (komut satırı gerekmez)

### 1) Ücretsiz hesaplar
1. GitHub'da ücretsiz hesap açın: https://github.com/signup
2. Kote Charts'ta ücretsiz hesap açın: https://kotecharts.com/signup
3. Kote hesabını e-posta ile doğrulayın ve hesap sayfasından `kote_...` ile başlayan API key üretin. Ücretsiz, kişisel/ticari olmayan kullanım içindir. API anahtarını kimseyle paylaşmayın.

### 2) GitHub deposu oluşturun
1. GitHub'a Windows bilgisayarınızdan giriş yapın.
2. Sağ üstte `+` → `New repository` seçin.
3. Repository name alanına `btc-institutional-demand` yazın.
4. `Public` seçin. GitHub Free hesapta ücretsiz Pages yayımlama için bu en kolay yoldur.
5. `Add a README file` işaretli kalsın ve `Create repository` seçin.
6. Depoda `Add file` → `Upload files` seçin.
7. Bu ZIP dosyasını Windows'ta sağ tıklayıp `Tümünü ayıkla` ile açın.
8. İçindeki `btc_demand_free` klasörünün **içindeki tüm dosya ve klasörleri** GitHub yükleme alanına sürükleyin. `.github` klasörünü de yükleyin. Windows Gezgini'nde gizli öğeleri göster seçeneğini açmanız gerekebilir.
9. Sayfanın altındaki `Commit changes` düğmesine basın.

### 3) Ücretsiz API anahtarını güvenli şekilde ekleyin
1. GitHub deposunda `Settings` → `Secrets and variables` → `Actions` bölümüne gidin.
2. `New repository secret` seçin.
3. Name alanına tam olarak `KOTE_API_KEY` yazın.
4. Secret alanına Kote hesabınızın API anahtarını yapıştırın.
5. `Add secret` seçin.

API anahtarını `update_data.py` dosyasına yazmayın ve herkese açık mesajlarda paylaşmayın.

### 4) GitHub Pages'i açın
1. Depoda `Settings` → `Pages` bölümüne gidin.
2. `Build and deployment` altında Source olarak `GitHub Actions` seçin.
3. Başka bir ayara dokunmanız gerekmez.
4. İlk başarılı iş akışı tamamlandığında GitHub Pages adresiniz bu sayfada görünür. Adres `https://KULLANICI-ADINIZ.github.io/btc-institutional-demand/` biçiminde olacaktır.

### 5) İlk veri güncellemesini başlatın
1. Depoda `Actions` sekmesine gidin.
2. Solda `Update BTC data and publish dashboard` iş akışını seçin.
3. `Run workflow` → `Run workflow` düğmesine basın.
4. İş akışında hem `build` hem `deploy` adımlarının yeşil onay işaretiyle tamamlandığını kontrol edin.
5. `Settings` → `Pages` bölümündeki `Visit site` bağlantısını açın. Panelde gerçek veriler görünmelidir.

Sonraki güncellemeler GitHub Actions tarafından yaklaşık 15 dakikada bir tetiklenir. GitHub zamanlanmış görevleri yoğunluk nedeniyle geciktirebilir; bu sistem saniyelik/al-sat amaçlı veri terminali değildir.

## Herkese açık depo uyarısı
Bu kurulumda GitHub deposu ve Pages paneli herkese açık olur. API anahtarı GitHub Secrets içinde şifreli saklanır; `data.json` içine yazılmaz. Depoya kişisel bilgi, borsa hesabı anahtarı veya cüzdan bilgisi koymayın.

## Bu sürümde henüz olmayanlar
- Otomatik emir gönderimi yok.
- Otomatik stop-loss / TP emri yok.
- Telegram bildirimleri yok.
- TradingView grafiğinin içine özel skor indikatörü olarak eklenmiş değil; panelde TradingView fiyat grafiği gömülüdür.
- LONG/SHORT etiketi yalnızca skor bölgesine göre genel eğilim filtresidir; giriş/çıkış emri değildir.

## Kaynaklar
- Binance public market data: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api
- Farside ETF flows: https://farside.co.uk/bitcoin-etf-flow-all-data/
- Kote exchange netflow: https://kotecharts.com/c/exchange-netflow
- Kote API docs: https://kotecharts.com/docs
- GitHub Pages: https://docs.github.com/en/pages
- GitHub Actions schedule: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
