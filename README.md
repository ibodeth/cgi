# cine-master

3D/CGI render motorlarından ve dijital prodüksiyon araçlarından çıkan ham videolara organik 35mm film dokusu kazandıran, sosyal medya (Instagram, TikTok, YouTube Shorts) sıkıştırmasında oluşan renk bantlaşmasını (color banding) azaltan ve dosyayı konteyner/bitstream düzeyinde temizleyerek teslime (delivery) hazırlayan bir Python CLI aracı.

Bütün piksel ve ses işlemleri **tek bir FFmpeg sürecinde**, `filter_complex` grafikleri ve bitstream filtreleri ile C hızında yapılır. Python tarafı yalnızca analiz (ffprobe), grafik üretimi, süreç yönetimi ve ilerleme göstergesiyle ilgilenir. OpenCV ile kare kare döngü kurulmaz, bu yüzden araya ek renk uzayı dönüşümü girmez.

---

## Gereksinimler

| Bileşen | Sürüm |
|---|---|
| Python | 3.10+ |
| FFmpeg / ffprobe | 5.1+ (`libx264` zorunlu; `libx265` ve `libsoxr` isteğe bağlı) |
| Python paketleri | `typer`, `rich`, `pydantic` v2 (kurulumla gelir) |

FFmpeg kurulumu:

```bash
# macOS
brew install ffmpeg
# Debian / Ubuntu
sudo apt install ffmpeg
# Windows (winget)
winget install Gyan.FFmpeg
```

## Kurulum

```bash
git clone <repo-url> cine-master
cd cine-master
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .            # geliştirme için: pip install -e '.[dev]'
cine-master --version
```

Kurulum yapmadan çalıştırmak için `python -m cinemaster ...` da kullanılabilir.

## Kullanım

```bash
# Tek dosya
cine-master process -i input.mp4 -o output.mp4 --preset cinema

# Klasördeki tüm .mp4/.mov/.mkv dosyaları, 2 paralel FFmpeg süreci
cine-master batch -d ./raw_renders -o ./mastered_renders --preset subtle --workers 2

# Parametre override örnekleri
cine-master process -i shot.mov -o shot_master.mp4 --preset cinema \
    --crop-pct 1.8 --grain 4 --crf 15 --fps 29.97 --keep-sei

# Encode etmeden üretilecek FFmpeg komutunu ve planı gör
cine-master process -i shot.mov -o shot.mp4 --dry-run

# Kaynak analizi ve preset tablosu
cine-master probe shot.mov
cine-master presets
```

### Komutlar

| Komut | Açıklama |
|---|---|
| `process -i IN -o OUT` | Tek dosya işler. Çıktı konteyneri uzantıdan seçilir (`.mp4`, `.m4v`, `.mov`, `.mkv`). |
| `batch -d DIR -o OUTDIR` | Klasördeki videoları paralel işler. `--workers N`, `--recursive`, `--container mp4\|mov\|mkv\|keep` (varsayılan `mp4`), `--suffix _master`. |
| `probe FILE` | Çözünürlük, döndürme, SAR, FPS, renk etiketleri ve ses bilgisini gösterir. |
| `presets` | Preset değerlerini tablo olarak listeler. |

### Seçenekler (`process` ve `batch` için ortak)

| Seçenek | Varsayılan | Açıklama |
|---|---|---|
| `--preset / -p` | `cinema` | `subtle`, `cinema`, `aggressive` |
| `--crop-pct` | 1.4 | Eksen başına toplam kırpma (%) |
| `--grain` | 3 | Luma greni gücü, 1-10 (`noise c0s`) |
| `--chroma-grain` | 1 | Kroma greni gücü, 0-10 (`noise c1s/c2s`) |
| `--unsharp` | 0.15 | Yeniden ölçekleme sonrası luma unsharp miktarı |
| `--deband` | 0.015 | `deband` eşiği (tüm düzlemler) |
| `--crf` | 16 | x264/x265 CRF |
| `--fps` | `source` | `source`, `24`, `25`, `29.97`, `30`, `23.976`, `59.94`, `30000/1001` … |
| `--codec` | `libx264` | `libx264` veya `libx265` |
| `--encoder-preset` | `slow` | x264/x265 hız preseti |
| `--keyint` | 2 × fps | GOP uzunluğu (30 fps'te 60) |
| `--strip-sei / --keep-sei` | strip | SEI NAL birimlerini bitstream'den çıkarır |
| `--audio-bitrate` | `256k` | AAC bit hızı |
| `--vendor` | `appl` | MOV video sample entry vendor 4CC'si (`''` = sıfır) |
| `--timescale` | otomatik | MP4/MOV video track timescale |
| `--seed` | yok | Kırpma/gren rastgeleliğini tekrarlanabilir yapar |
| `--overwrite / -y` | kapalı | Var olan çıktının üzerine yazar (batch'te kapalıysa atlanır) |
| `--dry-run` | kapalı | Komutu yazdırır, encode etmez |
| `--verbose / -v` | kapalı | Tam FFmpeg komutunu gösterir |

Tüm değerler `pydantic` ile doğrulanır. Aralık dışı bir değer (örn. `--grain 11`) encode başlamadan anlaşılır bir hata verir.

---

## İşleme pipeline'ı

Her dosya için `ffprobe` çalıştırılır, ardından tek bir FFmpeg komutu üretilir. Video ve ses dalları aynı süreçte iki bağımsız `-filter_complex` grafiği olarak verilir. Bunları `;` ile tek bir çok girişli grafikte birleştirmek, FFmpeg 6.1'de ProRes/PCM içeren interleaved MOV kaynaklarda takılmaya yol açıyor: bir dal, demuxer'ın diğer dal için beklettiği paketleri bekliyor. Bağımsız grafiklerde bu durum oluşmaz. Gerçek bir `--dry-run` çıktısından alınmış grafikler:

```
[0:0]fps=fps=30000/1001,
     crop=1262:710:12:4,
     scale=1280:720:flags=lanczos+accurate_rnd+full_chroma_int:in_range=auto:out_range=tv:in_color_matrix=auto:out_color_matrix=bt709,
     format=yuv420p,setsar=1,
     unsharp=lx=3:ly=3:la=0.15:cx=3:cy=3:ca=0.0,
     deband=1thr=0.015:2thr=0.015:3thr=0.015:range=16:blur=true,
     eq=contrast=1.015:brightness=0.002:saturation=1.01:gamma=0.99,
     noise=all_seed=1596953971:c0s=3:c0f=t+u:c1s=1:c1f=t+u:c2s=1:c2f=t+u[vout]

[0:1]aresample=48000:resampler=soxr:precision=28,highpass=f=22,lowpass=f=19600,
     allpass=f=1000:width_type=h:width=200,allpass=f=4000:width_type=h:width=500,
     asetrate=48029,aresample=48000:resampler=soxr:precision=28,atempo=0.9993961981,
     aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]
```

### 1. Konteyner ve bitstream temizliği

- `-map_metadata -1 -map_metadata:s -1 -map_chapters -1`: global, stream, chapter ve program meta verileri taşınmaz. Altyazı ve veri akışları (`-sn -dn`) ile kapak görselleri (attached pic) haritalanmaz.
- `-fflags +bitexact -flags:v +bitexact -flags:a +bitexact`: muxer ve encoder sürüm dizeleri (`Lavf…`, `Lavc…`) yazılmaz.
- `-bsf:v filter_units=remove_types=6` (H.264) ve `remove_types=39|40` (HEVC prefix/suffix SEI): x264/x265'in `user_data_unregistered` SEI'sindeki encoder imzası ve parametre dökümü bitstream'den silinir. x265 için ayrıca `info=0` verilir.
- `-movflags +faststart`: `moov` atomu dosyanın başına taşınır ve progressive/streaming oynatma hemen başlar.
- **Sample entry temizliği (`container.py`):** FFmpeg'in `-vendor` seçeneği yalnızca ProRes encoder'ının bir AVOption'ıdır ve libx264/x265 ile hiçbir şeyi değiştirmez. FFmpeg'in MOV muxer'ı ise video sample description'a sabit olarak `FFMP` vendor'ını ve 32 baytlık `compressorname` alanına `Lavc libx264` yazar. cine-master mux işleminden sonra `moov/trak/mdia/minf/stbl/stsd` ağacını okuyup bu sabit boyutlu alanları yerinde yeniden yazar: `compressorname` sıfırlanır, `.mov` için vendor `appl` olur (`--vendor` ile değiştirilebilir). `.mp4` dosyalarında ISO/IEC 14496-12 bu alanın sıfır kalmasını şart koştuğu için vendor'a dokunulmaz. Kutu boyutları ve sample offset'leri değişmez.

### 2. Asimetrik mikro-kırpma ve makroblok kaydırma

- `--crop-pct` eksen başına toplam bütçedir. Örneğin 1.5 değeri 1920 piksel genişlikte yaklaşık 29 piksele karşılık gelir.
- Bütçe her çalıştırmada ±%6 (`crop_jitter`) oynar. Sol/sağ ve üst/alt paylaşımı 50/50'den en az ~%5, en çok %15 (`crop_asymmetry`) sapacak biçimde rastgele bölünür. Örneğin: sol %0.94, sağ %0.47, üst %0.56, alt %0.83.
- Tüm ofsetler 4:2:0 kroma ızgarası için **çift sayıdır**. Sol ve üst ofset 8'in katına denk gelirse 2 piksel kaydırılır, böylece 8×8/16×16 blok ızgarası kaynaktaki konumunda kalmaz.
- Rastgelelik varsayılan olarak her çalıştırmada değişir. `--seed` verilirse sonuç birebir tekrarlanır.
- Kırpılan pencere `lanczos+accurate_rnd+full_chroma_int` ile orijinal çözünürlüğe geri ölçeklenir. 4:2:0 dönüşümü de bu Lanczos ölçekleyicinin içinde açıkça yapılır (`format=yuv420p`). Böylece FFmpeg grafiğin ilerisinde varsayılan bicubic ayarlı otomatik bir dönüştürücü eklemez.
- Hemen ardından hafif bir luma unsharp mask (`la=0.15`, kroma 0) uygulanır.
- Döndürülmüş kaynaklarda (telefon çekimi, `displaymatrix`) kırpma, FFmpeg'in autorotate sonrası kare boyutlarında hesaplanır. Kare olmayan SAR görüntü boyutuna çevrilir, tek sayılı boyutlar çift sayıya yuvarlanır (uyarı verilir).

### 3. De-banding, film greni ve kalibrasyon

Sıralama `deband → eq → noise` şeklindedir. `eq` 8-bit LUT ile çalıştığından yeniden kuantalama yapar ve yeni bantlar üretebilir. Gren en son katman olduğunda bu kuantalamayı da dither'lar. Ayrıca grenin genliği renk düzeltmesinden etkilenmez.

- `deband=…:blur=true`: düz gradyanlardaki basamaklar yumuşatılır.
- `eq=contrast=1.015:brightness=0.002:saturation=1.01:gamma=0.99`: algı eşiğinin altında mikro kalibrasyon.
- `noise=c0s=<grain>:c0f=t+u`: zamansal ve uniform luma greni. Kroma greni **her iki** kroma düzlemine (`c1s` ve `c2s`) eşit uygulanır. Yalnızca U'ya gren eklemek ton kaymasına yol açar. `all_seed` her çalıştırmada (veya `--seed` ile deterministik olarak) yeniden üretilir.

### 4. Zamansal örnekleme ve encoding

- `fps` filtresi kaynağı sabit kare hızına (CFR) sabitler. `--fps` verilirse hedef hıza yeniden örnekler. NTSC hızları tam rasyonel değerlerle işlenir (`29.97` → `30000/1001`).
- Kaynak FPS tespiti: CFR dosyada `r_frame_rate`, VFR dosyada `avg_frame_rate` kullanılır. Sonuç en yakın yayın standardına ±%0.05 toleransla oturtulur. 30 ile 29.97 arasındaki %0.1'lik fark bu toleransın dışında kaldığı için ikisi karışmaz.
- `-video_track_timescale`: her kare süresinin tam sayı tick'e denk geldiği bir zaman tabanı seçilir (24 fps → 12288, 29.97 → 30000).
- x264: `-preset slow -tune grain -crf 16 -profile:v high -pix_fmt yuv420p` ve
  `-x264-params aq-mode=3:aq-strength=0.8:psy-rd=1.0,0.15:deblock=-2,-2:keyint=<2×fps>:min-keyint=6`.
  `keyint` 30 fps'te tam 60'tır. Diğer hızlarda 2 saniyelik GOP korunur (24 fps → 48). İstenirse `--keyint 60` ile sabitlenebilir.
- x265 (`--codec libx265`): aynı AQ/psy/deblock değerleri, `-tag:v hvc1` (Apple/Instagram uyumu), `info=0`.
- Renk etiketleri (`-colorspace/-color_primaries/-color_trc/-color_range`) her zaman açıkça yazılır. HD, RGB ve etiketsiz kaynaklar BT.709 limited-range olarak işlenir, açıkça BT.601 etiketli kaynaklar BT.601 kalır. Full-range kaynaklar (`yuvj*`) Lanczos ölçekleyicide limited-range'e çevrilir. HDR (PQ/HLG) kaynaklar tone-mapping yapılmadan etiketleri korunarak işlenir ve uyarı verilir. Sosyal medya teslimi için önce SDR'a grade edilmeleri önerilir.

### 5. Ses mastering

Ses akışı ffprobe ile kontrol edilir. Ses yoksa grafik yalnızca videodan oluşur (`-an`).

1. `aresample=48000` (FFmpeg `libsoxr` ile derlenmişse SoX VHQ, değilse 64-tap swr)
2. `highpass=f=22, lowpass=f=19600`
3. `allpass=f=1000:width_type=h:width=200, allpass=f=4000:width_type=h:width=500`
4. `asetrate=48029, aresample=48000, atempo=48000/48029`. `asetrate` yalnızca tam sayı kabul ettiği için %0.06'lık kayma 48029 Hz'e yuvarlanır. Telafi edici `atempo` değeri de bu tam sayıdan hesaplanır, böylece süre örnek düzeyinde korunur.
5. `aformat=…:channel_layouts=stereo` → AAC-LC 48 kHz 256 kbps stereo. Mono kaynaklar ikiye çoğaltılır, 5.1 kaynaklar standart matrisle downmix edilir.

---

## Presetler

| parametre | subtle | cinema | aggressive |
|---|---|---|---|
| crop_pct | 1.0 | 1.4 | 2.2 |
| crop_asymmetry | 0.12 | 0.15 | 0.20 |
| unsharp_amount | 0.10 | 0.15 | 0.22 |
| deband_threshold | 0.010 | 0.015 | 0.020 |
| grain (luma) | 2 | 3 | 6 |
| chroma_grain | 0 | 1 | 2 |
| eq contrast / brightness | 1.008 / 0.001 | 1.015 / 0.002 | 1.03 / 0.004 |
| eq saturation / gamma | 1.005 / 0.995 | 1.01 / 0.99 | 1.02 / 0.98 |
| crf | 16 | 16 | 15 |

`cinema`, spesifikasyondaki referans değerlerin birebir karşılığıdır. CLI'dan verilen her seçenek preset değerini ezer.

## Sağlamlık

- **Atomik yazım:** FFmpeg çıktıyı önce `.<ad>.cm-xxxx.part.<ext>` adlı geçici dosyaya yazar. Encode ve header temizliği başarılı olursa dosya `os.replace` ile hedef adına taşınır. Hata ya da iptal durumunda geçici dosya silinir, yarım çıktı kalmaz.
- **İlerleme:** `-progress pipe:1` akışı okunur ve `rich` ilerleme çubuğu dosya süresine göre güncellenir. Batch modunda dosya başına bir çubuk ve bir toplam çubuğu gösterilir.
- **Hata raporu:** FFmpeg `stderr` çıktısının son 40 satırı tutulur ve hata panelinde ilgili komutla birlikte gösterilir.
- **Ctrl+C:** tüm FFmpeg süreçleri sonlandırılır, geçici dosyalar temizlenir, çıkış kodu 130 olur.
- **Batch:** `ThreadPoolExecutor` ile N paralel FFmpeg süreci çalışır (her biri kendi içinde çok iş parçacıklıdır, bu yüzden 1-3 worker genelde idealdir). Var olan çıktılar `--overwrite` verilmedikçe atlanır, çıktı klasörü kaynak klasörün içindeyse taramaya dahil edilmez.

## Bilinen sınırlar

- **MKV:** Matroska muxer'ı `bitexact` modunda bile `MuxingApp/WritingApp = Lavf` yazar ve bu alan cine-master tarafından yamalanmaz. İz bırakmayan teslim için `.mp4` (önerilen) veya `.mov` kullanın.
- Çıktı her zaman 8-bit 4:2:0'dır (sosyal platformların kabul ettiği format). 10-bit ve 4:4:4 kaynaklar Lanczos ölçekleyicide bir kez dönüştürülür.
- Yalnızca ilk video ve ilk ses akışı işlenir.

## Proje yapısı

```
cine-master/
├── cinemaster/
│   ├── __init__.py      # sürüm
│   ├── __main__.py      # python -m cinemaster
│   ├── cli.py           # Typer + Rich CLI (process, batch, probe, presets)
│   ├── probe.py         # ffprobe analizi: boyut, döndürme, SAR, FPS, renk, ses
│   ├── filters.py       # filter_complex, kırpma, renk ve bsf planlayıcı (saf fonksiyonlar)
│   ├── engine.py        # komut üretimi, subprocess, ilerleme, atomik yazım, batch
│   ├── container.py     # MP4/MOV sample entry (vendor/compressorname) temizleyici
│   └── presets.py       # pydantic MasterConfig + subtle/cinema/aggressive
├── tests/
│   ├── test_filters.py      # kırpma geometrisi, grafik sırası, FPS, renk, doğrulama
│   └── test_integration.py  # gerçek FFmpeg encode (FFmpeg yoksa atlanır)
├── pyproject.toml
└── README.md
```

## Test

```bash
pip install -e '.[dev]'
pytest
```

Entegrasyon testleri sentetik bir klip üretir, MP4 ve MOV çıktılarında `Lavf`, `Lavc`, `x264 - core` ve `FFMP` imzalarının ve kaynak meta verisinin kalmadığını, SEI sayısının sıfır olduğunu, çözünürlüğü ve 48 kHz stereo AAC çıktıyı doğrular.
