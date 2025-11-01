# Telegram Bot Kurulum Rehberi

## Adım 1: Telegram Bot Oluşturma

1. Telegram'ı açın ve **@BotFather**'ı arayın
2. `/start` komutunu gönderin
3. `/newbot` komutunu gönderin
4. Bot'unuza bir isim verin (örnek: "My Arbitrage Bot")
5. Bot'unuza bir kullanıcı adı verin (örnek: "my_arb_bot")
6. BotFather size bir **token** verecek. Bu token'ı kaydedin!
   - Format: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

## Adım 2: Chat ID Bulma

### Yöntem 1: @userinfobot Kullanma
1. Telegram'da **@userinfobot**'u arayın
2. `/start` gönderin
3. Bot size **Chat ID**'nizi verecek (örnek: `987654321`)

### Yöntem 2: @RawDataBot Kullanma
1. Telegram'da **@RawDataBot**'u arayın
2. Herhangi bir mesaj gönderin
3. JSON yanıtında `"id"` değerini bulun

### Yöntem 3: Manuel Olarak
1. Bot'unuza bir mesaj gönderin (örnek: "/start")
2. Tarayıcıda şu URL'yi açın (TOKEN'ı kendi token'ınızla değiştirin):
   ```
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```
3. Dönen JSON'da `"chat":{"id":987654321}` şeklinde Chat ID'yi bulun

## Adım 3: .env Dosyasını Düzenleme

`.env` dosyasını açın ve şu satırları doldurun:

```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=987654321
```

## Adım 4: Bot'u Test Etme

1. Docker container'ları yeniden başlatın:
   ```bash
   docker compose down
   docker compose up --build
   ```

2. Bot başladığında Telegram'da size bir bildirim gelmeli

3. Bot'unuza `/help` komutu gönderin - eğer yanıt alırsanız, başarılı! 🎉

## Sorun Giderme

### Bot yanıt vermiyor
- Token'ın doğru olduğundan emin olun (boşluk vs. olmamalı)
- Chat ID'nin doğru olduğundan emin olun
- Bot'a önce siz mesaj göndermelisiniz (`/start`)
- Docker loglarını kontrol edin: `docker compose logs worker`

### "Unauthorized" hatası
- Bot token'ı yanlış, @BotFather'dan kontrol edin

### Bildirimler gelmiyor
- Chat ID yanlış olabilir
- Bot'a `/start` gönderdiğinizden emin olun
- .env dosyasında TELEGRAM_CHAT_ID boş olabilir

## Örnek Kullanım

Bot çalışır durumda olduğunda şu komutları deneyebilirsiniz:

```
/status          → Bot durumu
/balance         → Sermaye bakiyeleri
/trades          → Son 1 saatteki trade'ler
/trades 6        → Son 6 saatteki trade'ler
/pnl             → Son 24 saat PNL özeti
/pnl 48          → Son 48 saat PNL özeti
/positions       → Açık pozisyonlar
/stats           → Genel istatistikler
/rebalance       → Sermaye dengeleme kontrolü
```

## Güvenlik Notu

⚠️ **ÖNEMLİ**: Bot token'ınızı ve chat ID'nizi kimseyle paylaşmayın!
- Bu bilgiler bot'unuza tam erişim sağlar
- .env dosyasını git'e commit etmeyin
- Eğer token sızdıysa, @BotFather üzerinden `/revoke` ile iptal edip yeni token alın

## Otomatik Bildirimler

Bot kurulumu tamamlandıktan sonra, aşağıdaki durumlarda otomatik bildirim alacaksınız:

- ✅ **Başarılı trade** - Direction, edge, hacim
- ❌ **Başarısız trade** - Hata detayları
- 💰 **Pozisyon kapandı** - PNL, süre, edge decay
- ⚖️ **Auto-rebalance** - Sermaye yeniden dağıtıldı
- 🛑 **Bot durdu** - Kritik hata veya manuel durdurma

Artık trade'lerinizi cebinizden takip edebilirsiniz! 📱
