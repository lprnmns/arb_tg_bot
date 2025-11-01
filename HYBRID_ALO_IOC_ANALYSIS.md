# HYBRID ALO/IOC STRATEJİ ANALİZİ

## 📋 ÖNERİLEN PLAN

### Açılış Stratejisi:
```
1. Edge >= threshold → ALO emri gönder
2. 150ms bekle
3. Fill oldu mu?
   ✅ YES → Devam et
   ❌ NO  → İptal et + IOC ile aç
```

### Kapanış Stratejisi:
```
1. Pozisyon kapatma zamanı geldi
2. ALO emri gönder
3. 5000ms bekle
4. Fill oldu mu?
   ✅ YES → Devam et
   ❌ NO  → İptal et + IOC ile kapat
```

---

## 🔬 BİLİMSEL ANALİZ

### A) MALİYET KARŞILAŞTIRMASI

#### Senaryo 1: %100 IOC (Mevcut)
```
Açılış:  11.5 bps
Kapanış: 11.5 bps
Slippage: 10 bps (aggressive pricing)
────────────────────
TOPLAM:  43 bps
```

#### Senaryo 2: %100 ALO (İdeal)
```
Açılış:  5.5 bps
Kapanış: 5.5 bps
Slippage: 0 bps (passive pricing)
────────────────────
TOPLAM:  11 bps ✅ 32 bps tasarruf!
```

#### Senaryo 3: Hybrid - ALO Success Rates
```
Açılış ALO success: X%
Kapanış ALO success: Y%

Beklenen maliyet =
  (X% × 11 bps) +           # Full ALO
  ((1-X%) × 43 bps)         # ALO fail → IOC

Example:
• 50% ALO success → 27 bps avg (16 bps tasarruf)
• 70% ALO success → 19.9 bps avg (23.1 bps tasarruf)
• 90% ALO success → 14.2 bps avg (28.8 bps tasarruf)
```

---

### B) RİSK ANALİZİ

#### Risk 1: PARTIAL FILL ⚠️ KRİTİK
```
Problem:
  Perp emri doldu (SHORT $19)
  Spot emri dolmadı (BUY $0)

→ HEDGE YOK! Naked SHORT position!
→ Fiyat yükselirse ZARAR

Çözüm:
  Atomic check: Her iki taraf da doldu mu?
  Yoksa → Hemen IOC ile eksik tarafı kapat
```

#### Risk 2: SPREAD KAPANMASI 📉
```
Senaryo:
  t=0ms:   Edge = 25 bps → ALO gönder
  t=50ms:  Edge = 15 bps (spread kapandı)
  t=100ms: Edge = 5 bps
  t=150ms: Timeout → IOC ile aç

→ Açılış: 5 bps edge (çok düşük!)
→ Kapanış: -11.5 bps maliyet
→ NET: -6.5 bps ZARAR

Olasılık: YÜKSEK (HYPE volatil)
```

#### Risk 3: QUEUE POSİTİON 📊
```
ALO = maker order = kuyruğa gir

Eğer spread'de önünde çok emir varsa:
  → Fill olmaz
  → 150ms timeout
  → IOC'ye düşer

HYPE likidite: Orta
→ 20-50 trade/saat
→ Queue competitive
```

#### Risk 4: ADVERSE SELECTION 🎯
```
ALO dolarsa ne zaman dolar?
→ Spread BANA GELDİĞİNDE (worst case!)

Example:
  Bid: 44.50 (benim ALO sell)
  Ask: 44.60

Fiyat 44.70'e çıkıyor → Benim 44.50 emrim doluyor
→ HEMEN fiyat düşüyor (market reversal)
→ Kapanışta zarar

IOC ile:
  → Garantili fill
  → Predictable slippage
  → No adverse selection
```

---

### C) PERFORMANS HESAPLAMALARI

#### Metrik 1: Break-even ALO Success Rate
```
IOC maliyet: 43 bps
ALO maliyet: 11 bps
Fark: 32 bps

Break-even hesabı:
  X × 11 + (1-X) × 43 = 43
  11X + 43 - 43X = 43
  -32X = 0
  X = 0%

Yani: ALO %0 başarı olsa bile, hybrid strateji asla daha kötü olamaz!
```

#### Metrik 2: Karlılık Artışı
```
20 bps threshold ile:

IOC-only (mevcut):
  Net PNL: 20 - 43 = -23 bps ZARAR ❌

Hybrid %50 ALO:
  Net PNL: 20 - 27 = -7 bps ZARAR ❌ (ama daha az!)

Hybrid %70 ALO:
  Net PNL: 20 - 19.9 = +0.1 bps ✅ Break-even!

Hybrid %90 ALO:
  Net PNL: 20 - 14.2 = +5.8 bps KARLI ✅✅
```

#### Metrik 3: Gerçekçi ALO Success Rate (HYPE)
```
Faktörler:
  ✅ HYPE spread: Dar (2-5 bps avg)
  ✅ Likidite: Orta-yüksek ($50K+ daily vol)
  ⚠️  Volatilite: Yüksek
  ⚠️  Edge duration: Kısa (1-5 saniye)

Tahmini:
  Açılış ALO success: 30-50%  (spread hızlı kapanır)
  Kapanış ALO success: 60-80% (kapanırken acele yok)

Weighted avg: ~50% success rate
→ Net maliyet: 27 bps
→ Hala 20 bps threshold ile karlı DEĞİL (-7 bps)
```

---

### D) ZAMANLAMA ANALİZİ

#### 150ms Timeout (Açılış)
```
HYPE edge duration:
  • P50: 1-3 saniye
  • P90: 5-10 saniye
  • P95: 10+ saniye

150ms içinde:
  → %20-30 spread değişir
  → %70-80 spread stabil kalır

KRİTİK NOKTA:
  150ms KISA → Çoğu ALO dolmaz → IOC'ye düşer
  500ms UZUN → Spread kapanır → Zarar

Optimal: 200-300ms?
```

#### 5000ms Timeout (Kapanış)
```
Kapanışta acele yok:
  • Pozisyon zaten açık
  • Hedge var
  • Edge 0'a yakın → stabil

5000ms MANTIKLI ✅
  → ALO dolma şansı yüksek (%70-80)
  → Risk düşük (pozisyon zaten hedge)
```

---

### E) DEADMAN MEKANİĞİ İLE UYUM

Mevcut sistem:
```python
# Deadman: Edge >= threshold olarak KALMA süresi
deadman_ms = 1000  # 1 saniye stabil olmalı

if edge >= threshold:
    if stabil_kalma_süresi >= 1000ms:
        → Trade tetikle
```

Hybrid eklersek:
```python
# 1. Deadman check (1000ms)
if edge_stable_for_1000ms():

    # 2. ALO dene (200ms)
    send_alo_orders()
    wait(200)

    # 3. Fill check
    if both_filled():
        ✅ Başarılı (11 bps)
    else:
        cancel_alo()
        send_ioc_orders()  # 43 bps

Total delay: 1000 + 200 = 1200ms
```

**Sorun:**
- 1200ms sonra edge hala var mı? ⚠️
- Spread kapanmış olabilir

---

## 🎯 ÖNERİLER

### Öneri 1: KAPANIşTA ALO ⭐ MANTIKLI
```
✅ Düşük risk (hedge zaten var)
✅ Yüksek success rate (%70-80)
✅ 5.5 bps tasarruf
✅ 5 saniye timeout yeterli

Implementation:
  position_manager.py:
    close_method = "alo_with_ioc_fallback"
    alo_timeout_ms = 5000
```

### Öneri 2: AÇILIŞTA HIBRID 🤔 RİSKLİ AMA TEST EDİLEBİLİR
```
⚠️  Risk: Spread kapanması
⚠️  Risk: Partial fill
✅ Potansiyel: 5.5-16 bps tasarruf

Koşullar:
  1. Deadman'i KISALT (1000ms → 500ms)
  2. ALO timeout KISALT (150ms → 100ms)
  3. Spread check: Dar mı? (< 5 bps)
  4. Edge check: Hala >= threshold mı?

Implementation:
  execution.py:
    if spread < 5_bps AND edge >= threshold:
        try_alo_first = True
        alo_timeout_ms = 100
```

### Öneri 3: THRESHOLDʼU YÜKSELTİP IOC ⭐⭐ EN GÜVENLİ
```
✅ Sıfır risk
✅ Predictable PNL
✅ Garantili hedge
✅ Simple logic

45 bps threshold + IOC:
  Net PNL: 45 - 43 = +2 bps ✅
  Günlük 1-2 trade → Aylık +%3-5
```

---

## 📊 TEST PLANI

### Faz 1: BACKTEST (Simülasyon)
```
1. Geçmiş edge verilerini topla (1-2 gün)
2. Simüle et:
   • %100 IOC
   • %100 ALO
   • Hybrid (100ms, 200ms, 300ms timeout)
3. Karşılaştır:
   • Fill rate
   • Net PNL
   • Risk events
```

### Faz 2: LIVE TEST - Kapanış ALO
```
Config:
  open_method: "ioc"
  close_method: "alo_with_fallback"
  close_alo_timeout: 5000ms

Duration: 24 saat
Track:
  • ALO fill rate
  • ALO fill time (p50, p90, p95)
  • IOC fallback rate
  • Net cost per trade
```

### Faz 3: LIVE TEST - Açılış Hybrid
```
Config:
  open_method: "hybrid"
  open_alo_timeout: 200ms
  close_method: "alo_with_fallback"

Duration: 24 saat
Track:
  • Partial fill events
  • Spread closure events
  • Edge degradation
  • Net PNL
```

---

## 🧮 SONUÇ

### Matematiksel Gerçekler:
```
1. IOC maliyet: 43 bps ❌ Pahalı
2. ALO maliyet: 11 bps ✅ Ucuz
3. 20 bps threshold: İkisiyle de ZARAR
4. Break-even IOC: 43 bps threshold
5. Break-even ALO: 11 bps threshold
```

### Risk/Reward:
```
KAPANIŞ ALO:
  Risk:  Düşük (hedge var)
  Reward: 5.5 bps tasarruf
  Verdict: ✅ IMPLEMENT

AÇILIŞ HYBRID:
  Risk:  Orta-yüksek (spread, partial fill)
  Reward: 5.5-16 bps tasarruf
  Verdict: ⚠️  TEST CAREFULLY

THRESHOLD ARTIR:
  Risk:  Yok
  Reward: Garantili karlılık
  Verdict: ✅✅ EN GÜVENLİ
```

### Nihai Öneri:
```
1. THRESHOLD'u 45-50 bps'e çıkar (IMMEDIATE)
2. Kapanışta ALO kullan (LOW RISK)
3. 1-2 hafta veri topla
4. Açılış hybrid'i test et (IF data supports)
```

---

## 💻 IMPLEMENTATION CHECKLIST

- [ ] execution.py: Add `alo_with_ioc_fallback` mode
- [ ] position_manager.py: Use ALO for closes
- [ ] Add timeout mechanism (asyncio.wait_for)
- [ ] Add atomic fill check (both or neither)
- [ ] Add spread monitoring
- [ ] Add fill time tracking
- [ ] Update config: close_alo_timeout_ms
- [ ] Add metrics: alo_success_rate, avg_fill_time
- [ ] Telegram alerts: ALO success/fail events

---

## 🔬 AÇIK SORULAR

1. **Geçmiş ALO fill time verisi var mı?**
   - Yoksa → Önce data collection
   - Varsa → Direkt analiz

2. **Deadman süresi esneklik verir mi?**
   - Kısaltabilirsek → ALO hybrid daha mantıklı
   - Kısaltamazsak → Sadece kapanış ALO

3. **Spread genellikle ne kadar süre stabil?**
   - 1+ saniye → ALO şansı yüksek
   - <500ms → ALO şansı düşük

4. **Risk toleransın ne?**
   - Yüksek → Hybrid test et
   - Düşük → Sadece threshold artır
