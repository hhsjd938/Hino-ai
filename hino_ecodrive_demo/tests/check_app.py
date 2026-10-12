"""Streamlit application smoke checks, without requiring a browser or audio device."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_xlsx_import import workbook_bytes
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def verify(app):
    assert not app.exception, [error.message for error in app.exception]
    assert not app.error, [error.value for error in app.error]
    components = [element for element in app.main if element.type == "bidi_component"]
    assert components, [element.type for element in app.main]


if __name__ == "__main__":
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    verify(app)
    print("PASS real trip")
    app.radio(key="data_mode").set_value("模擬情境").run()
    for key in ("normal", "high_rpm", "high_rpm_low_speed", "acceleration", "deceleration", "idle", "speeding"):
        app.selectbox(key="scenario").set_value(key).run()
        verify(app)
        print(f"PASS scenario {key}")
    app.number_input[0].set_value(11.0).run()
    verify(app)
    print("PASS parameter change")
    app.radio(key="data_mode").set_value("真實 HINO 行程").run()
    app.radio(key="replay_source").set_value("上傳 CSV").run()
    assert not app.exception and not app.error
    assert any(element.type == 'file_uploader' for element in app.main)
    assert any('請先選擇 CSV' in item.value for item in app.info)
    print("PASS upload entry and waiting state")
    content = (APP.parent / 'replay.csv').read_bytes()
    uploaded = SimpleNamespace(name='uploaded_trip.csv', getvalue=lambda: content)
    with patch('streamlit.file_uploader', return_value=uploaded):
        app.run()
        verify(app)
        assert any('已匯入 uploaded_trip.csv' in item.value for item in app.success)
    print("PASS uploaded CSV dashboard (file selection mocked)")
    with patch('streamlit.file_uploader', return_value=SimpleNamespace(name='bad.csv', getvalue=lambda: b'bad')):
        app.run()
        assert not app.exception
        assert any('CSV 缺少欄位' in error.value for error in app.error)
    print("PASS invalid upload error message")
    app.radio(key="replay_source").set_value("上傳 XLSX").run()
    assert not app.exception and not app.error
    assert any('請先選擇 XLSX' in item.value for item in app.info)
    rows = [['timestamp','speed','rpm','car_status','can_status','vehicle','journey'],
            ['2025-01-01 08:00:00',20,1500,1,0,'A','001'],
            ['2025-01-01 08:00:01',22,1600,1,0,'A','001'],
            ['2025-01-01 08:00:00',30,1500,1,0,'B','002'],
            ['2025-01-01 08:00:01',32,1600,1,0,'B','002']]
    excel = workbook_bytes({'車況':rows,'說明':[['notes']]})
    with patch('streamlit.file_uploader', return_value=SimpleNamespace(name='trips.xlsx', getvalue=lambda: excel)):
        app.run()
        verify(app)
        assert app.selectbox(key='xlsx_vehicle').value == 'A'
        app.selectbox(key='xlsx_vehicle').set_value('B').run()
        verify(app)
        assert app.selectbox(key='xlsx_journey').value == '002'
        app.selectbox(key='xlsx_sheet').set_value('說明').run()
        assert not app.exception and any('缺少欄位' in error.value for error in app.error)
        app.selectbox(key='xlsx_sheet').set_value('車況').run()
        verify(app)
    print("PASS XLSX upload, vehicle/trip selection and sheet recovery (file selection mocked)")
    with patch('streamlit.file_uploader', return_value=SimpleNamespace(name='broken.xlsx', getvalue=lambda: b'broken')):
        app.run()
        assert not app.exception
        assert any('無法讀取 XLSX' in error.value for error in app.error)
    print("PASS invalid XLSX error message")
    app.radio(key="replay_source").set_value("內建範例").run()
    verify(app)
    app.radio(key="feature").set_value("共同起終點搜尋").run()
    assert not app.exception and not app.error
    app.selectbox(key='od_seed').set_value(('AHMPUL0C13','250111000001')).run()
    app.button(key='od_fill').click().run()
    next(button for button in app.button if button.label=="搜尋共同起終點").click().run()
    assert not app.exception, [error.message for error in app.exception]
    assert not app.error, [error.value for error in app.error]
    assert len(app.dataframe)==2
    assert any(element.type=='deck_gl_json_chart' for element in app.main)
    assert int(app.metric[0].value)>=36
    print("PASS common OD search, group summary and map")
    next(button for button in app.button if button.label=="交換起點與終點").click().run()
    next(button for button in app.button if button.label=="搜尋共同起終點").click().run()
    assert not app.exception and not app.error
    print("PASS reverse-direction search")
    app.number_input(key='od_start_lat').set_value(0.0)
    app.number_input(key='od_start_lon').set_value(0.0)
    next(button for button in app.button if button.label=="搜尋共同起終點").click().run()
    assert not app.exception and not app.error
    assert any('沒有符合條件' in warning.value for warning in app.warning)
    print("PASS no-result search")
