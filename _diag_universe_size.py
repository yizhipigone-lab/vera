"""确认 4 个股票池的实际规模 (尤其 list_type=51 是创业板指成分~100 还是全创业板~1300)。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher

TdxConnector.initialize()
for lt, name in [('23','沪深300'), ('24','中证500'), ('51','创业板'), ('5','全A'), ('50','沪深A股')]:
    try:
        codes = DataFetcher.get_stock_universe(lt)
        print(f"list_type={lt} ({name}): {len(codes)} 只", flush=True)
    except Exception as e:
        print(f"list_type={lt} ({name}) 失败: {e}", flush=True)
TdxConnector.close()
