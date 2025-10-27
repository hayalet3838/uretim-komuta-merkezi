import os
import datetime
from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from typing import Optional, List
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor, RealDictRow
from pathlib import Path
import logging
from pydantic import BaseModel, Field
import re 

# --- LOGLAMA AYARLARI ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__) 

# Uvicorn loglarını kısıtla
logging.getLogger("uvicorn.access").setLevel(logging.WARNING) 
logging.getLogger("uvicorn.error").setLevel(logging.INFO)


# --- TEMEL AYARLAR ---
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = Path(BASE_DIR, "static") 

app = FastAPI(
    title="Üretim Yönetim Paneli - API",
    json_encoders={
        datetime.date: lambda dt: dt.strftime("%Y-%m-%d"),
        datetime.datetime: lambda dt: dt.isoformat(),
    }
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(Path(BASE_DIR, "templates")))


# --- VERİTABANI BAĞLANTISI ---
DB_NAME = os.getenv("DB_NAME", "uretimm_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1234") 
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432") 

db_pool = None

try:
    db_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=1, maxconn=20, host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASSWORD, port=DB_PORT, cursor_factory=RealDictCursor
    )
    if db_pool:
        logger.info("INFO: Veritabanı bağlantı havuzu başarıyla oluşturuldu.")
except Exception as e:
    logger.error(f"HATA: Veritabanı bağlantısı kurulamadı: {e}")

# --- YARDIMCI FONKSİYONLAR ---
def get_db_conn():
    if db_pool: return db_pool.getconn()
    raise HTTPException(status_code=503, detail="Veritabanı bağlantısı yok.")

def release_db_conn(conn):
    if db_pool and conn: db_pool.putconn(conn)

def json_compatible_data(data):
    """
    Tarih/zaman nesnelerini ISO string'e çevirir. 
    """
    if isinstance(data, (datetime.date, datetime.datetime)):
        return data.isoformat()
    if isinstance(data, (list, tuple)):
        return [json_compatible_data(item) for item in data]
    if isinstance(data, (dict, RealDictRow)):
        return {k: json_compatible_data(v) for k, v in data.items()}
    return data

def run_db_query(conn, query, params=None, fetch='none'):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = None
            
            if fetch == 'one':
                row = cur.fetchone()
                result = dict(row) if isinstance(row, RealDictRow) else None
            elif fetch == 'all':
                rows = cur.fetchall()
                result = [dict(r) for r in rows if isinstance(r, RealDictRow)]
            
            if query.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE")):
                 conn.commit()
                 
            if result is not None:
                 return json_compatible_data(result)
            return result
            
    except psycopg2.Error as db_err:
        conn.rollback()
        logger.error(f"VERİTABANI HATASI ({db_err.pgcode}): {db_err.pgerror}")
        raise HTTPException(status_code=500, detail=f"Veritabanı sorgusu başarısız: {db_err.pgerror}")
    except Exception as e:
        conn.rollback()
        logger.error(f"Genel Hata: {e}")
        raise HTTPException(status_code=500, detail=f"Genel sunucu hatası: {e}")

# --- YARDIMCI FONKSİYON: METİNDEN SAYI ÇEKME ---
def extract_number_from_package(package_str: Optional[str]) -> int:
    """Paket metninden (örn: "10 çuval") tamsayıyı çeker. Çekemezse 0 döndürür."""
    if not package_str:
        return 0
    # Metnin başından veya bir boşluktan sonra gelen sayıları arar
    match = re.search(r'^\s*(\d+)|(?<=\s)(\d+)', package_str)
    if match:
        return int(match.group(1) or match.group(2)) 
    return 0


# --- Pydantic Modelleri ---

class KayitTemel(BaseModel):
    aciklama: str
    paket_sayisi: Optional[str] = None
    adet: int = Field(..., gt=0)

class Kayit(KayitTemel):
    id: int
    tarih: str
    toplam_uretim: int

class SiparisTemel(BaseModel):
    musteri_adi: str
    urun_adi: str
    hedef_adet: int = Field(..., gt=0)
    siparis_tarihi: str 
    durum: str = 'Aktif'
    parca_adeti_per_takim: int = Field(1, gt=0)

class Siparis(SiparisTemel):
    id: int

class SiparisDetay(Siparis):
    uretilen: int
    ilerleme_yuzdesi: float
    toplam_parca_hedefi: int 
    toplam_uretilen_parca: int


# --- UYGULAMA BAŞLANGIÇ TABLO KONTROLÜ (DB Temizliği Dahil) ---
@app.on_event("startup")
def startup_db_check():
    conn = None
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()
        logger.info("INFO: Başlangıç DB kontrolü yapılıyor ve eski veriler temizleniyor...")
        
        # Her seferinde veritabanını sıfırlamak (Hata ve Veri Kalıntılarını Önlemek İçin)
        try:
             cur.execute("TRUNCATE uretim_kayitlari, siparisler RESTART IDENTITY CASCADE;") 
             conn.commit()
             logger.warning("UYARI: Tüm Sipariş ve Kayıt verileri veritabanından silindi ve sayaclar sıfırlandı.")
        except Exception as e:
             logger.error(f"TRUNCATE Hatası: {e}")
             conn.rollback()
             
        # Tablo Oluşturma SQL'leri
        cur.execute("""
            CREATE TABLE IF NOT EXISTS siparisler (
                id SERIAL PRIMARY KEY,
                musteri_adi VARCHAR(255) NOT NULL,
                urun_adi VARCHAR(255) NOT NULL,
                siparis_tarihi DATE DEFAULT CURRENT_DATE,
                hedef_adet INTEGER NOT NULL CHECK (hedef_adet > 0),
                durum VARCHAR(50) DEFAULT 'Aktif',
                parca_adeti_per_takim INTEGER DEFAULT 1 NOT NULL CHECK (parca_adeti_per_takim > 0)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS uretim_kayitlari (
                id SERIAL PRIMARY KEY,
                tarih TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                aciklama TEXT NOT NULL,
                paket_sayisi VARCHAR(100),
                adet INTEGER NOT NULL CHECK (adet >= 0)
            );
        """)
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_schema = 'public' AND table_name = 'uretim_kayitlari' AND column_name = 'siparis_id') THEN
                    ALTER TABLE uretim_kayitlari ADD COLUMN siparis_id INTEGER REFERENCES siparisler(id) ON DELETE SET NULL;
                    RAISE NOTICE 'uretim_kayitlari tablosuna siparis_id sütunu eklendi.';
                END IF;
            END $$;
        """)
        
        # İlk Test Verilerini Ekleme 
        cur.execute("SELECT COUNT(*) FROM siparisler;")
        count = cur.fetchone()['count']
        
        if count == 0:
            cur.execute("INSERT INTO siparisler (musteri_adi, urun_adi, hedef_adet, parca_adeti_per_takim, siparis_tarihi) VALUES (%s, %s, %s, %s, %s);", ('ABC Enerji', 'Kalorimetre Takımı', 10000, 7, datetime.date.today()))
            cur.execute("INSERT INTO siparisler (musteri_adi, urun_adi, hedef_adet, parca_adeti_per_takim, siparis_tarihi) VALUES (%s, %s, %s, %s, %s);", ('XYZ İnşaat', 'Vana Seti', 5000, 1, datetime.date.today()))
            logger.info("INFO: Siparisler tablosuna ilk test kayıtları yeniden eklendi.")

        conn.commit()
        cur.close()
        logger.info("INFO: Veritabanı tabloları ve ilk veriler kontrol edildi/oluşturuldu.")
    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"HATA: Başlangıç DB kontrol hatası: {e}")
    finally:
        if conn: db_pool.putconn(conn)


# --- API ENDPOINT'LERİ ---

@app.get("/", response_class=HTMLResponse)
async def get_root(request: Request):
    return templates.TemplateResponse("panel_obsidian.html", {"request": request})

# 1. TÜM SİPARİŞLERİ GETİR (Dropdown için)
@app.get("/api/siparisler", response_model=List[Siparis])
async def get_siparisler(conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    try:
        query = "SELECT id, musteri_adi, urun_adi, hedef_adet, siparis_tarihi, durum, parca_adeti_per_takim FROM siparisler ORDER BY id DESC;"
        siparisler = run_db_query(conn, query, fetch='all')
        
        for siparis in siparisler:
             siparis['siparis_tarihi'] = json_compatible_data(siparis['siparis_tarihi'])
             
        return siparisler if siparisler else []
    except Exception as e:
        logger.error(f"Siparişler alınamadı: {e}")
        raise HTTPException(status_code=500, detail=f"Siparişler alınamadı: {e}")
    finally:
        release_db_conn(conn)

# 2. YENİ SİPARİŞ OLUŞTUR (POST)
@app.post("/api/siparisler", status_code=status.HTTP_201_CREATED, response_model=Siparis)
async def create_siparis(siparis: SiparisTemel, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    try:
        try:
             siparis_tarihi_date = datetime.date.fromisoformat(siparis.siparis_tarihi)
        except ValueError:
             raise HTTPException(status_code=400, detail="Sipariş tarihi geçerli bir formatta değil (YYYY-MM-DD bekleniyor).")
             
        query = """
            INSERT INTO siparisler (musteri_adi, urun_adi, hedef_adet, siparis_tarihi, durum, parca_adeti_per_takim)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, musteri_adi, urun_adi, hedef_adet, siparis_tarihi, durum, parca_adeti_per_takim;
        """
        result = run_db_query(conn, query, params=(
            siparis.musteri_adi,
            siparis.urun_adi,
            siparis.hedef_adet,
            siparis_tarihi_date,
            siparis.durum,
            siparis.parca_adeti_per_takim
        ), fetch='one')

        if not result:
            raise HTTPException(status_code=500, detail="Sipariş oluşturuldu ancak veri alınamadı.")
            
        return Siparis(**result)

    except Exception as e:
        logger.error(f"Sipariş oluşturma hatası: {e}")
        raise HTTPException(status_code=500, detail=f"Sipariş oluşturma hatası: {e}")
    finally:
        release_db_conn(conn)

# 3. SİPARİŞİ GÜNCELLE (PUT)
@app.put("/api/siparisler/{siparis_id}", response_model=Siparis)
async def update_siparis(siparis_id: int, siparis: SiparisTemel, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    try:
        check_query = "SELECT id FROM siparisler WHERE id = %s;"
        if not run_db_query(conn, check_query, params=(siparis_id,), fetch='one'):
            raise HTTPException(status_code=404, detail="Güncellenecek sipariş bulunamadı.")

        try:
             siparis_tarihi_date = datetime.date.fromisoformat(siparis.siparis_tarihi)
        except ValueError:
             raise HTTPException(status_code=400, detail="Sipariş tarihi geçerli bir formatta değil (YYYY-MM-DD bekleniyor).")

        query = """
            UPDATE siparisler SET
                musteri_adi = %s,
                urun_adi = %s,
                hedef_adet = %s,
                siparis_tarihi = %s,
                durum = %s,
                parca_adeti_per_takim = %s
            WHERE id = %s
            RETURNING id, musteri_adi, urun_adi, hedef_adet, siparis_tarihi, durum, parca_adeti_per_takim;
        """
        result = run_db_query(conn, query, params=(
            siparis.musteri_adi,
            siparis.urun_adi,
            siparis.hedef_adet,
            siparis_tarihi_date,
            siparis.durum,
            siparis.parca_adeti_per_takim,
            siparis_id
        ), fetch='one')

        return Siparis(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Sipariş güncelleme hatası: {e}")
        raise HTTPException(status_code=500, detail=f"Sipariş güncelleme hatası: {e}")
    finally:
        release_db_conn(conn)


# 4. SİPARİŞ DETAYINI GETİR (Headerlar için)
@app.get("/api/siparisler/{siparis_id}/detay", response_model=SiparisDetay)
async def get_siparis_detay(siparis_id: int, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    try:
        siparis_sorgu = "SELECT id, musteri_adi, urun_adi, hedef_adet, siparis_tarihi, durum, parca_adeti_per_takim FROM siparisler WHERE id = %s;"
        siparis_detay = run_db_query(conn, siparis_sorgu, params=(siparis_id,), fetch='one')

        if not siparis_detay:
            raise HTTPException(status_code=404, detail="Sipariş bulunamadı.")

        toplam_uretim_sorgu = "SELECT COALESCE(SUM(adet), 0) as uretilen_adet FROM uretim_kayitlari WHERE siparis_id = %s;"
        toplam_uretilen_data = run_db_query(conn, toplam_uretim_sorgu, params=(siparis_id,), fetch='one')
        
        uretilen_adet = toplam_uretilen_data['uretilen_adet'] if toplam_uretilen_data else 0
        hedef_adet = siparis_detay['hedef_adet']
        parca_adeti = siparis_detay['parca_adeti_per_takim']

        ilerleme_yuzdesi = round((uretilen_adet / hedef_adet) * 100, 2) if hedef_adet > 0 else 0
        ilerleme_yuzdesi = min(ilerleme_yuzdesi, 100)
        
        toplam_parca_hedefi = hedef_adet * parca_adeti
        toplam_uretilen_parca = uretilen_adet * parca_adeti
        
        current_durum = siparis_detay['durum']
        if uretilen_adet >= hedef_adet and current_durum == 'Aktif':
            update_durum_query = "UPDATE siparisler SET durum = 'Tamamlandı' WHERE id = %s;"
            run_db_query(conn, update_durum_query, params=(siparis_id,))
            siparis_detay['durum'] = 'Tamamlandı'

        # Tarihi string formatına çevir (DD.MM.YYYY)
        siparis_tarihi_iso = siparis_detay.get('siparis_tarihi')
        try:
             siparis_tarihi_str = datetime.date.fromisoformat(siparis_tarihi_iso).strftime('%d.%m.%Y')
        except ValueError:
             siparis_tarihi_str = '--.--.----'

        result = {
            'id': siparis_detay.get('id'),
            'musteri_adi': siparis_detay.get('musteri_adi'),
            'urun_adi': siparis_detay.get('urun_adi'),
            'hedef_adet': hedef_adet,
            'siparis_tarihi': siparis_tarihi_str, 
            'durum': siparis_detay.get('durum'),
            'parca_adeti_per_takim': parca_adeti,
            'uretilen': uretilen_adet,
            'ilerleme_yuzdesi': ilerleme_yuzdesi,
            'toplam_parca_hedefi': toplam_parca_hedefi, 
            'toplam_uretilen_parca': toplam_uretilen_parca,
        }
        
        return SiparisDetay(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Sipariş detayları alınamadı: {e}")
        raise HTTPException(status_code=500, detail=f"Sipariş detayları alınamadı: {e}")
    finally:
        release_db_conn(conn)


# 5. YENİ ÜRETİM KAYDI OLUŞTUR / GÜNCELLE (POST/PUT)
@app.post("/api/kayitlar/{siparis_id}", status_code=status.HTTP_201_CREATED, response_model=Kayit)
async def create_kayit(siparis_id: int, kayit: KayitTemel, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    
    try:
        check_query = "SELECT id FROM siparisler WHERE id = %s;"
        if not run_db_query(conn, check_query, params=(siparis_id,), fetch='one'):
            raise HTTPException(status_code=404, detail="Kayıt eklenmek istenen sipariş bulunamadı.")
            
        # --- KRİTİK ADIM: BOŞLUK TEMİZLEME ---
        temiz_aciklama = kayit.aciklama.strip()
        
        if not temiz_aciklama:
            raise HTTPException(status_code=400, detail="Açıklama alanı boş bırakılamaz.")
            
        # --- MANTIK: AYNI SİPARİŞ ve AYNI AÇIKLAMA KONTROLÜ ---
        
        kontrol_sorgu = """
            SELECT id, adet, paket_sayisi, tarih
            FROM uretim_kayitlari 
            WHERE siparis_id = %s 
              AND aciklama = %s                 
            ORDER BY tarih DESC, id DESC LIMIT 1;
        """
        
        mevcut_kayit = run_db_query(conn, kontrol_sorgu, params=(siparis_id, temiz_aciklama), fetch="one")

        yeni_id = None
        result_tarih = None
        
        if mevcut_kayit:
            # Durum 1: Mevcut Kayıt Var -> ÜZERİNE GÜNCELLEME YAP
            
            eski_adet = mevcut_kayit.get('adet', 0)
            yeni_adet = eski_adet + kayit.adet # SADECE ADETİ TOPLA
            
            eski_paket_metin = mevcut_kayit.get('paket_sayisi', '')
            yeni_paket_metin = kayit.paket_sayisi if kayit.paket_sayisi else ''
            
            # --- PAKET SAYILARINI ÇEK VE TOPLA ---
            
            eski_paket_sayi = extract_number_from_package(eski_paket_metin)
            yeni_paket_sayi = extract_number_from_package(yeni_paket_metin)
            toplam_paket_sayi = eski_paket_sayi + yeni_paket_sayi
            
            # Birim bilgisini al (Yeni paketten, yoksa eskisinden)
            birim_suffix = ""
            if yeni_paket_metin:
                sayi_match = re.search(r'^\s*(\d+)|(?<=\s)(\d+)', yeni_paket_metin)
                if sayi_match:
                    sayi_str = sayi_match.group(1) or sayi_match.group(2)
                    birim_suffix = yeni_paket_metin.replace(sayi_str, '').strip()
            
            if not birim_suffix and eski_paket_metin:
                sayi_match = re.search(r'^\s*(\d+)|(?<=\s)(\d+)', eski_paket_metin)
                if sayi_match:
                    sayi_str = sayi_match.group(1) or sayi_match.group(2)
                    birim_suffix = eski_paket_metin.replace(sayi_str, '').strip()

            # Yeni metni oluştur (örn: "12 çuval")
            if toplam_paket_sayi > 0:
                 yeni_paket_db = f"{toplam_paket_sayi}{' ' if birim_suffix else ''}{birim_suffix}".strip()
            else:
                 yeni_paket_db = None
            
            # --- PAKET SAYILARINI ÇEK VE TOPLA SONU ---

            kayit.paket_sayisi = yeni_paket_db # Pydantic modeline güncellenmiş değeri ata
            
            guncelleme_sorgu = """
                UPDATE uretim_kayitlari 
                SET adet = %s, paket_sayisi = %s, tarih = CURRENT_TIMESTAMP
                WHERE id = %s 
                RETURNING id, tarih;
            """
            result = run_db_query(conn, guncelleme_sorgu, params=(yeni_adet, yeni_paket_db, mevcut_kayit['id']), fetch="one")
            
            if not result:
                raise HTTPException(status_code=500, detail="Mevcut kayıt güncellenemedi.")
                
            yeni_id = result['id']
            result_tarih = result['tarih']
            kayit.adet = yeni_adet 
            kayit.aciklama = temiz_aciklama 
            
        else:
            # Durum 2: Mevcut Kayıt Yok -> YENİ KAYIT OLUŞTUR
            yeni_kayit_sorgu = """
                INSERT INTO uretim_kayitlari (siparis_id, aciklama, paket_sayisi, adet)
                VALUES (%s, %s, %s, %s) 
                RETURNING id, tarih;
            """
            yeni_paket_db = kayit.paket_sayisi.strip() if kayit.paket_sayisi else None
            
            result = run_db_query(conn, yeni_kayit_sorgu, params=(
                siparis_id,
                temiz_aciklama,  
                yeni_paket_db, 
                kayit.adet
            ), fetch="one")

            if not result:
                 raise HTTPException(status_code=500, detail="Kayıt eklendi ancak ID alınamadı.")

            yeni_id = result['id']
            result_tarih = result['tarih']
            kayit.aciklama = temiz_aciklama 
            kayit.paket_sayisi = yeni_paket_db

        # --- ORTAK KISIM: TOPLAM ÜRETİMİ HESAPLA VE SONUCU DÖNDÜR ---
        
        toplam_sorgu = "SELECT COALESCE(SUM(adet), 0) as toplam FROM uretim_kayitlari WHERE siparis_id = %s;"
        toplam_result = run_db_query(conn, toplam_sorgu, params=(siparis_id,), fetch="one")
        guncel_toplam_uretim = toplam_result['toplam'] if toplam_result else kayit.adet

        return Kayit(
            id=yeni_id,
            tarih=datetime.datetime.fromisoformat(result_tarih).strftime('%d.%m.%Y %H:%M'),
            aciklama=kayit.aciklama,
            paket_sayisi=kayit.paket_sayisi,
            adet=kayit.adet, 
            toplam_uretim=guncel_toplam_uretim
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Kayıt eklenirken/güncellenirken bir hata oluştu: {e}")
        raise HTTPException(status_code=500, detail=f"Kayıt eklenirken/güncellenirken bir hata oluştu: {e}")
    finally:
        release_db_conn(conn)

# 6. SİPARİŞE AİT KAYITLARI GETİR
@app.get("/api/kayitlar/{siparis_id}", response_model=List[Kayit])
async def get_siparis_kayitlari(siparis_id: int, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    
    try:
        # SQL sorgusu, her kaydın eklendiği ana kadar olan toplam üretimi (koşullu pencere fonksiyonu ile) hesaplar
        query = """
            SELECT 
                k.id, k.tarih, k.aciklama, k.paket_sayisi, k.adet,
                SUM(k2.adet) FILTER (WHERE k2.tarih <= k.tarih AND k2.siparis_id = k.siparis_id) OVER (ORDER BY k.tarih, k.id) as toplam_uretim
            FROM uretim_kayitlari k
            JOIN uretim_kayitlari k2 ON k.siparis_id = k2.siparis_id
            WHERE k.siparis_id = %s
            ORDER BY k.tarih DESC, k.id DESC;
        """
        kayitlar = run_db_query(conn, query, params=(siparis_id,), fetch='all')

        if not kayitlar:
            return []

        for kayit in kayitlar:
            # Tarih formatını kullanıcı dostu hale getir
            kayit['tarih'] = datetime.datetime.fromisoformat(kayit['tarih']).strftime('%d.%m.%Y %H:%M') if kayit.get('tarih') else '--.--.---- --:--'

        return [Kayit(**k) for k in kayitlar]
    
    except Exception as e:
        logger.error(f"Kayıtlar alınamadı: {e}")
        raise HTTPException(status_code=500, detail=f"Kayıtlar alınamadı: {e}")
    finally:
        release_db_conn(conn)
 
# --- API: KAYIT DÜZENLEME VE SİLME ENDPOINTLERİ ---

# 7. TEK BİR ÜRETİM KAYDINI GETİR (Düzenleme Modalı için)
@app.get("/api/kayitlar/detay/{kayit_id}", response_model=KayitTemel)
async def get_kayit_detay(kayit_id: int, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    try:
        query = "SELECT aciklama, paket_sayisi, adet FROM uretim_kayitlari WHERE id = %s;"
        kayit = run_db_query(conn, query, params=(kayit_id,), fetch='one')
        
        if not kayit:
            raise HTTPException(status_code=404, detail="Kayıt bulunamadı.")
            
        return KayitTemel(**kayit)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Kayıt detayları alınamadı: {e}")
        raise HTTPException(status_code=500, detail=f"Kayıt detayları alınamadı: {e}")
    finally:
        release_db_conn(conn)


# 8. TEK BİR ÜRETİM KAYDINI GÜNCELLE (Düzenleme Modalı Kaydet)
@app.put("/api/kayitlar/{kayit_id}", response_model=Kayit)
async def update_kayit(kayit_id: int, kayit_temel: KayitTemel, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    try:
        # Kaydın siparis_id'sini ve eski değerlerini al
        check_query = "SELECT siparis_id, adet, paket_sayisi FROM uretim_kayitlari WHERE id = %s;"
        mevcut_kayit = run_db_query(conn, check_query, params=(kayit_id,), fetch='one')
        
        if not mevcut_kayit:
            raise HTTPException(status_code=404, detail="Güncellenecek kayıt bulunamadı.")

        siparis_id = mevcut_kayit['siparis_id']
        temiz_aciklama = kayit_temel.aciklama.strip()
        yeni_paket_db = kayit_temel.paket_sayisi.strip() if kayit_temel.paket_sayisi else None

        # Sadece bu kaydı güncelle (Adet ve Paket Sayısı)
        update_query = """
            UPDATE uretim_kayitlari 
            SET aciklama = %s, paket_sayisi = %s, adet = %s, tarih = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id, tarih;
        """
        result = run_db_query(conn, update_query, params=(
            temiz_aciklama, 
            yeni_paket_db, 
            kayit_temel.adet, 
            kayit_id
        ), fetch='one')

        if not result:
            raise HTTPException(status_code=500, detail="Kayıt güncellendi ancak dönüş verisi alınamadı.")

        # Toplam üretimi yeniden hesapla (Çünkü tek bir kayıt değişti)
        toplam_sorgu = "SELECT COALESCE(SUM(adet), 0) as toplam FROM uretim_kayitlari WHERE siparis_id = %s;"
        toplam_result = run_db_query(conn, toplam_sorgu, params=(siparis_id,), fetch="one")
        guncel_toplam_uretim = toplam_result['toplam']

        return Kayit(
            id=kayit_id,
            tarih=datetime.datetime.fromisoformat(result['tarih']).strftime('%d.%m.%Y %H:%M'),
            aciklama=temiz_aciklama,
            paket_sayisi=yeni_paket_db,
            adet=kayit_temel.adet, 
            toplam_uretim=guncel_toplam_uretim
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Kayıt güncelleme hatası: {e}")
        raise HTTPException(status_code=500, detail=f"Kayıt güncelleme hatası: {e}")
    finally:
        release_db_conn(conn)
        
# 9. ÜRETİM KAYDI SİL
@app.delete("/api/kayitlar/{kayit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kayit(kayit_id: int, conn: psycopg2.extensions.connection = Depends(get_db_conn)):
    try:
        # Kaydın varlığını kontrol et
        check_query = "SELECT siparis_id FROM uretim_kayitlari WHERE id = %s;"
        result = run_db_query(conn, check_query, params=(kayit_id,), fetch='one')
        
        if not result:
            raise HTTPException(status_code=404, detail="Silinecek kayıt bulunamadı.")
            
        # Kaydı sil
        delete_query = "DELETE FROM uretim_kayitlari WHERE id = %s;"
        run_db_query(conn, delete_query, params=(kayit_id,))
        
        logger.info(f"Kayıt silindi: ID {kayit_id}")
        return {} # 204 No Content için boş dönüş

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Kayıt silme hatası: {e}")
        raise HTTPException(status_code=500, detail=f"Kayıt silme hatası: {e}")
    finally:
        release_db_conn(conn)
                

# --- Uygulamayı Çalıştırma ---
if __name__ == "__main__":
    import uvicorn
    # Yerel çalıştırma için
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
