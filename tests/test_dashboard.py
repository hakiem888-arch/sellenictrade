from pathlib import Path
from streamlit.testing.v1 import AppTest
from futures.market_scanner import MarketScanner
from tests.fixtures import NOW

APP=Path(__file__).resolve().parents[1]/'app.py'


def by_label(elements,label): return next(x for x in elements if x.label==label)


def test_dashboard_needs_no_pair_or_api_inputs_and_can_stop(monkeypatch,tmp_path):
    monkeypatch.setenv('FUTURES_HISTORY_DB',str(tmp_path/'paper.db'))
    calls=[]
    def run(self,config,manual=False):
        calls.append(config)
        return {'now_ms':NOW,'universe_count':300,'eligible_count':50,'shortlist':[],
                'selected':[],'analyses':[],'errors':[],'duration_seconds':1}
    monkeypatch.setattr(MarketScanner,'run',run)
    at=AppTest.from_file(str(APP),default_timeout=20).run()
    assert not at.exception
    assert not at.text_input
    assert not any(x.label=='Mode' for x in at.selectbox)
    assert by_label(at.toggle,'Auto-scan seluruh market').value
    assert by_label(at.selectbox,'Interval scan (detik)').options==['30','45','60','90']
    assert [x.label for x in at.tabs]==['Scanner market','Paper trading','Riwayat analisis','Aturan']
    assert len(calls)==1
    at.run(); assert len(calls)==1
    by_label(at.selectbox,'Interval scan (detik)').set_value(30).run()
    assert not at.exception and calls[-1].interval==30
    by_label(at.toggle,'Auto-scan seluruh market').set_value(False).run()
    count=len(calls)
    at.session_state['market_schedule'].next_due=0
    at.run(); assert len(calls)==count
    by_label(at.button,'Scan sekarang').click().run()
    assert not at.exception and len(calls)==count+1


def test_dashboard_renders_error_and_journal_backup(monkeypatch,tmp_path):
    monkeypatch.setenv('FUTURES_HISTORY_DB',str(tmp_path/'paper.db'))
    monkeypatch.setattr(MarketScanner,'run',lambda *_args,**_kwargs:{'now_ms':NOW,'errors':['WAIT: HTTP 451']})
    at=AppTest.from_file(str(APP),default_timeout=20).run()
    assert not at.exception
    assert any('451' in x.value for x in at.warning)
    assert any(x.label=='Unduh cadangan jurnal lengkap' for x in at.get('download_button'))
