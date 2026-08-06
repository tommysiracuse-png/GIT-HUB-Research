from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.mongolian_stock_exchange_comex import (
    DASHBOARD_URL,
    SOURCE_URL,
    MongolianStockExchangeComexAdapter,
    parse_mongolian_stock_exchange_comex_dashboard,
    parse_mongolian_stock_exchange_comex_notice_index,
    parse_mongolian_stock_exchange_comex_notice_page,
)


DASHBOARD_PAYLOAD = """
{
  "tableData": [
    {
      "auctionId": 4318,
      "productNumber": 2826,
      "productTypeNameMN": "Нүүрс",
      "productTypeNameEN": "Coal",
      "productTypeNameCN": "煤炭",
      "auctionStatus": 1,
      "productPrice": 63.7,
      "orderPrice": 63.7,
      "currency": "USD",
      "sellerNameMN": "Эрдэнэс Тавантолгой ХК",
      "sellerNameEN": "Erdenes Tavantolgoi JSC",
      "sellerNameCN": "埃尔登内斯·塔万陶勒盖有限责任公司",
      "auctionStartTime": "2026-08-06 12:00:00",
      "size": 10,
      "lot_price": "6400.00",
      "wetSize": 0
    },
    {
      "auctionId": 4316,
      "productNumber": 2824,
      "productTypeNameMN": "Нүүрс",
      "productTypeNameEN": "Coal",
      "productTypeNameCN": "煤炭",
      "auctionStatus": 3,
      "productPrice": 90.5,
      "orderPrice": 120.5,
      "currency": "USD",
      "sellerNameMN": "Эрдэнэс Тавантолгой ХК",
      "sellerNameEN": "Erdenes Tavantolgoi JSC",
      "sellerNameCN": "埃尔登内斯·塔万陶勒盖有限责任公司",
      "auctionStartTime": "2026-08-06 10:00:00",
      "size": 40,
      "lot_price": "6400.00",
      "wetSize": 0
    }
  ],
  "serverTime": "2026-08-06T04:53:23.116103Z"
}
"""

NOTICE_INDEX = """
<html><body>
  <div class="flex-grow-1 ms-3">
    <h6 class="mb-1 lh-base"><a href="/show-article/1028" class="text-reset">“ЭНЕРЖИ РЕСУРС” ХХК-ИЙН 2026 ОНЫ 05 ДУГААР САРЫН 29 БОЛОН 06 ДУГААР САРЫН 02, 03, 05-НЫ ӨДРҮҮДИЙН ДУУДЛАГА АРИЛЖААНЫ ҮНЭ ӨӨРЧЛӨГДЛӨӨ</a></h6>
    <p class="text-muted fs-12 mb-0">2026-05-28 15:56:12</p>
  </div>
  <div class="flex-grow-1 ms-3">
    <h6 class="mb-1 lh-base"><a href="/show-article/1025" class="text-reset">МЭДЭГДЭЛ</a></h6>
    <p class="text-muted fs-12 mb-0">2026-05-12 14:59:12</p>
  </div>
  <div class="flex-grow-1 ms-3">
    <h6 class="mb-1 lh-base"><a href="/show-article/1022" class="text-reset">“МОНГОЛЫН АЛТ” (МАК) ХХК-ИЙН СПОТ ГЭРЭЭНИЙ СЭХЭ БООМТЫН НӨХЦӨЛТЭЙ 1/3 КОКСЖИХ НҮҮРСНИЙ ДУУДЛАГА АРИЛЖАА 2026 ОНЫ 05 ДУГААР САРЫН 12-НЫ ӨДӨР ЗОХИОН БАЙГУУЛАГДАНА.</a></h6>
    <p class="text-muted fs-12 mb-0">2026-05-05 12:09:50</p>
  </div>
</body></html>
"""

PRICE_CHANGE_NOTICE = """
<html><body>
  <div class="card-header">
    <h3 class="card-title mb-2">“ЭНЕРЖИ РЕСУРС” ХХК-ИЙН 2026 ОНЫ 05 ДУГААР САРЫН 29 БОЛОН 06 ДУГААР САРЫН 02, 03, 05-НЫ ӨДРҮҮДИЙН ДУУДЛАГА АРИЛЖААНЫ ҮНЭ ӨӨРЧЛӨГДЛӨӨ</h3>
    <h6 class="card-subtitle font-14 text-muted">2026-05-28 15:56:12</h6>
  </div>
  <div class="card-body">
    <article>
      <figure class="table">
        <table>
          <tbody>
            <tr>
              <td><strong>Арилжааны хуваарь</strong></td>
              <td><strong>Бүтээгдэхүүний нэр, төрөл</strong></td>
              <td><strong>Хэмжээ</strong></td>
              <td><strong>Хуучин нөхцөл</strong></td>
              <td><strong>Шинэчлэгдсэн нөхцөл</strong></td>
            </tr>
            <tr>
              <td>2026.05.29-ний 10:00 цаг</td>
              <td>Нүүрс, Баяжуулсан коксжих нүүрс</td>
              <td>12,800 тонн</td>
              <td>900 CNY</td>
              <td><strong>950 CNY</strong></td>
            </tr>
            <tr>
              <td>2026.06.02-ны 10:00 цаг</td>
              <td>Нүүрс, Баяжуулсан коксжих нүүрс</td>
              <td>12,800 тонн</td>
              <td>1070 CNY</td>
              <td><strong>1120 CNY</strong></td>
            </tr>
          </tbody>
        </table>
      </figure>
    </article>
  </div>
</body></html>
"""

SPOT_NOTICE = """
<html><body>
  <div class="card-header">
    <h3 class="card-title mb-2">“МОНГОЛЫН АЛТ” (МАК) ХХК-ИЙН СПОТ ГЭРЭЭНИЙ СЭХЭ БООМТЫН НӨХЦӨЛТЭЙ 1/3 КОКСЖИХ НҮҮРСНИЙ ДУУДЛАГА АРИЛЖАА 2026 ОНЫ 05 ДУГААР САРЫН 12-НЫ ӨДӨР ЗОХИОН БАЙГУУЛАГДАНА.</h3>
    <h6 class="card-subtitle font-14 text-muted">2026-05-05 12:09:50</h6>
  </div>
  <div class="card-body">
    <article>
      <p>“Монголын Алт” (МАК) ХХК-ийн 1/3 коксжих нүүрсний дуудлага арилжаа нь 2026 оны 05 дугаар сарын 12-ны өдрийн 10:00 цагаас Уул уурхайн бүтээгдэхүүний арилжааны систем (Comex.mse.mn)-ээр зохион байгуулагдахаар боллоо.</p>
      <p>Тус компанийн биржээр дамжуулан арилжаалах 1/3 коксжих нүүрс нь 4 багц буюу 25,600 тонн бөгөөд дуудлага арилжааны эхлэх үнэ тонн тутмын 470 юань байна. Нүүрсний нийлүүлэлтийг 2026 оны 06 дугаар сарын 11-ний өдөр буюу арилжаа зохион байгуулагдснаас хойш 30 хоногийн хугацаанд бүрэн гүйцэтгэж Шивээхүрэн->Сэхэ боомтоор дамжуулан худалдан авагч талд хүргэж дуусгах спот гэрээний арилжаа гэдгээрээ онцлогтой юм.</p>
    </article>
  </div>
</body></html>
"""

RESCHEDULE_NOTICE = """
<html><body>
  <div class="card-header">
    <h3 class="card-title mb-2">МЭДЭГДЭЛ</h3>
    <h6 class="card-subtitle font-14 text-muted">2026-05-12 14:59:12</h6>
  </div>
  <div class="card-body">
    <article>
      <p>МХБ-ийн уул уурхайн бүтээгдэхүүний арилжааны системийн дэд бүтэц байрладаг “Үндэсний дата төв” дээр интернет сүлжээний доголдол гарсантай холбоотойгоор 2026 оны 05-р сарын 12-ны өдрийн 12:00 цагийн “Эрдэнэс Тавантолгой” ХК-ийн 10 багц 64,000 тонн 1/3 коксжих нүүрсний арилжааг цуцалж, тус арилжааг өмнөх нөхцөлийг өөрчлөлгүйгээр мөн өдрийн буюу 2026 оны 05 дугаар сарын 12-ны өдрийн 15:00 цагаас дахин зохион байгуулах болсныг мэдэгдье.</p>
    </article>
  </div>
</body></html>
"""


def text_result(text: str, received_at: str = "2026-08-06T04:55:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class MongolianStockExchangeComexAdapterTests(unittest.TestCase):
    def test_parsers_normalize_dashboard_and_notice_pages(self) -> None:
        dashboard_rows = parse_mongolian_stock_exchange_comex_dashboard(
            DASHBOARD_PAYLOAD,
            received_at="2026-08-06T04:55:00+00:00",
        )
        self.assertEqual(2, len(dashboard_rows))
        completed = {row["auction_id"]: row for row in dashboard_rows}[4316]
        self.assertEqual(120.5, completed["last"])
        self.assertEqual(40 * 6400.0, completed["total_tonnage"])
        self.assertEqual("completed", completed["session_status"])
        self.assertEqual("official_dashboard_auction_snapshot", completed["quality_status"])

        notice_links = parse_mongolian_stock_exchange_comex_notice_index(
            NOTICE_INDEX,
            source_url=SOURCE_URL,
            limit=3,
        )
        self.assertEqual(
            [
                "https://comex.mse.mn/show-article/1028",
                "https://comex.mse.mn/show-article/1025",
                "https://comex.mse.mn/show-article/1022",
            ],
            [item["article_url"] for item in notice_links],
        )

        price_change_rows = parse_mongolian_stock_exchange_comex_notice_page(
            PRICE_CHANGE_NOTICE,
            source_url="https://comex.mse.mn/show-article/1028",
            received_at="2026-08-06T04:55:00+00:00",
        )
        self.assertEqual(2, len(price_change_rows))
        self.assertEqual(950.0, price_change_rows[0]["last"])
        self.assertEqual(50.0, price_change_rows[0]["published_price_delta_per_tonne"])
        self.assertEqual("CNY", price_change_rows[0]["quote"])

        spot_rows = parse_mongolian_stock_exchange_comex_notice_page(
            SPOT_NOTICE,
            source_url="https://comex.mse.mn/show-article/1022",
            received_at="2026-08-06T04:55:00+00:00",
        )
        self.assertEqual(1, len(spot_rows))
        self.assertEqual(470.0, spot_rows[0]["last"])
        self.assertEqual(25600.0, spot_rows[0]["total_tonnage"])
        self.assertEqual("CNY", spot_rows[0]["quote"])
        self.assertEqual("official_spot_contract_notice", spot_rows[0]["quality_status"])

        reschedule_rows = parse_mongolian_stock_exchange_comex_notice_page(
            RESCHEDULE_NOTICE,
            source_url="https://comex.mse.mn/show-article/1025",
            received_at="2026-08-06T04:55:00+00:00",
        )
        self.assertEqual(1, len(reschedule_rows))
        self.assertEqual("rescheduled", reschedule_rows[0]["session_status"])
        self.assertEqual(64000.0, reschedule_rows[0]["total_tonnage"])
        self.assertFalse(reschedule_rows[0]["price_available"])

    def test_runtime_discovery_scan_and_failure_evidence(self) -> None:
        adapter_id = "mongolian_stock_exchange_comex"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, MongolianStockExchangeComexAdapter)
        self.assertEqual("MSE_COMEX", adapter.info.venue)
        self.assertEqual(SOURCE_URL, adapter.info.docs_url)
        self.assertIn("auction_price_change", adapter.info.capabilities)
        self.assertIn("public_market_data", adapter.info.capabilities)

        with mock.patch(
            "adapters.venues.mongolian_stock_exchange_comex.fetch_text",
            side_effect=[
                text_result(DASHBOARD_PAYLOAD),
                text_result(NOTICE_INDEX),
                text_result(PRICE_CHANGE_NOTICE),
                text_result(RESCHEDULE_NOTICE),
                text_result(SPOT_NOTICE),
            ],
        ):
            batch = MongolianStockExchangeComexAdapter().scan(
                {
                    "public_market_adapters": {
                        "mongolian_stock_exchange_comex": {
                            "max_notice_documents": 3,
                        }
                    }
                }
            )
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(6, batch.metadata["real_observation_count"])
        self.assertEqual(3, batch.metadata["notice_document_count"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["dashboard_table"]["fetch_status"])
        self.assertTrue(batch.metadata["paper_only"])
        priced = [row for row in batch.observations if row.get("last", 0) > 0]
        self.assertEqual(5, len(priced))

        original_discover = adapter_runtime.discover_adapters
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.mongolian_stock_exchange_comex.fetch_text",
            side_effect=[
                text_result(DASHBOARD_PAYLOAD),
                text_result(NOTICE_INDEX),
                text_result(PRICE_CHANGE_NOTICE),
                text_result(RESCHEDULE_NOTICE),
                text_result(SPOT_NOTICE),
            ],
        ), mock.patch.object(
            adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)
        ), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(
            adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"
        ), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(
            adapter_runtime,
            "discover_adapters",
            side_effect=lambda: [item for item in original_discover() if item == adapter_id],
        ):
            runtime_batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0, "max_notice_documents": 3}},
                    }
                }
            )
        report = runtime_batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertEqual(5, report["adapters"][0]["price_observation_count"])

        with mock.patch(
            "adapters.venues.mongolian_stock_exchange_comex.fetch_text",
            side_effect=[
                {"ok": False, "status": "blocked", "http_status": 403, "text": "", "received_at": "2026-08-06T04:55:00+00:00", "latency_ms": 5.0, "error": "HTTP Error 403"},
                {"ok": False, "status": "blocked", "http_status": 403, "text": "", "received_at": "2026-08-06T04:55:00+00:00", "latency_ms": 5.0, "error": "HTTP Error 403"},
            ],
        ):
            failed = MongolianStockExchangeComexAdapter().scan({})
        self.assertEqual("blocked", failed.metadata["source_status"])
        self.assertEqual(0, failed.metadata["real_observation_count"])
        self.assertEqual(2, len(failed.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in failed.observations))
        self.assertTrue(
            all(row["candidate_reject_reason"] == "public_mse_comex_source_unavailable" for row in failed.observations)
        )

    def test_capability_reconciliation_matches_spec_402(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #402: Mongolian Stock Exchange Comex",
                "market_key": "global_discovery|Mongolian Stock Exchange Comex",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Mongolian Stock Exchange Comex",
                        "public_docs_url": SOURCE_URL,
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("mongolian_stock_exchange_comex", match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
