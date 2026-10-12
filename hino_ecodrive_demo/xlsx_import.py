"""Stream XLSX records into compressed chunks, then replay one selected trip."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import posixpath
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, from_excel

from csv_import import analyze_uploaded
from engine import read_replay_bytes
from prepare_replay import MAPPING, NS, cell_value, shared_strings

MAX_XLSX_BYTES = 200 * 1024 * 1024
CHUNK_ROWS = 2000
FIELDS = list(MAPPING.values()) + ['event_reports', 'source_excel_row']
REQUIRED = {'timestamp', 'speed', 'rpm', 'car_status', 'can_status'}
REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def sheet_info(content: bytes) -> dict:
    if len(content) > MAX_XLSX_BYTES:
        raise ValueError('XLSX 大小需在 200 MB 以內')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as book:
            workbook = ET.fromstring(book.read('xl/workbook.xml'))
            relationships = ET.fromstring(book.read('xl/_rels/workbook.xml.rels'))
            targets = {}
            for relationship in relationships:
                if relationship.get('TargetMode') == 'External' or not relationship.get('Type', '').endswith('/worksheet'):
                    continue
                target = relationship.get('Target', '')
                targets[relationship.get('Id')] = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
            sheets = {sheet.get('name'): targets[sheet.get(REL_NS + 'id')]
                      for sheet in workbook.findall(NS + 'sheets/' + NS + 'sheet')
                      if sheet.get(REL_NS + 'id') in targets}
            if not sheets or any(path not in book.namelist() for path in sheets.values()):
                raise ValueError('找不到可讀取的工作表')
            properties = workbook.find(NS + 'workbookPr')
            mac_dates = properties is not None and properties.get('date1904', '').lower() in ('1', 'true')
            return {'sheets': sheets, 'mac_dates': mac_dates}
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as error:
        raise ValueError('無法讀取 XLSX；請確認是未加密、未損毀的 .xlsx 檔案') from error


def timestamp(value, mac_dates=False) -> str:
    text = str(value or '').strip()
    try:
        return datetime.fromisoformat(text).isoformat(sep=' ')
    except ValueError:
        try:
            serial = float(text)
            if not math.isfinite(serial):
                raise ValueError()
            result = from_excel(serial, CALENDAR_MAC_1904 if mac_dates else CALENDAR_WINDOWS_1900)
            if not isinstance(result, datetime):
                raise ValueError()
            return result.isoformat(sep=' ')
        except (ValueError, OverflowError, TypeError) as error:
            raise ValueError(f'無效日期時間：{text or "空白"}') from error


def identifier(value) -> str:
    text = str(value or '').strip()
    # Excel may serialize integer event/trip ids as 6.0; preserve textual leading zeros.
    if text.endswith('.0') and text[:-2].isdigit():
        return text[:-2]
    return text


def read_sheet(content: bytes, sheet: str) -> dict:
    info = sheet_info(content)
    if sheet not in info['sheets']:
        raise ValueError('找不到所選工作表')
    trip_index, chunks, batch, batch_trips = {}, [], [], set()
    columns = None
    count = 0

    def flush():
        if not batch:
            return
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(batch)
        chunk_id = len(chunks)
        chunks.append(gzip.compress(buffer.getvalue().encode('utf-8'), mtime=0))
        for trip_key in batch_trips:
            trip_index[trip_key]['chunks'].append(chunk_id)
        batch.clear()
        batch_trips.clear()

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as book:
            strings = shared_strings(book)
            with book.open(info['sheets'][sheet]) as stream:
                sheet_data = None
                for event, row in ET.iterparse(stream, events=('start', 'end')):
                    if event == 'start' and row.tag == NS + 'sheetData':
                        sheet_data = row
                    if event != 'end' or row.tag != NS + 'row':
                        continue
                    values = {}
                    for cell in row.findall(NS + 'c'):
                        col = ''.join(c for c in cell.get('r', '') if c.isalpha())
                        if columns is None or col in columns:
                            values[col] = cell_value(cell, strings)
                    excel_row = row.get('r', '?')
                    row.clear()
                    if sheet_data is not None:
                        sheet_data.clear()
                    if not any(value not in (None, '') for value in values.values()):
                        continue
                    if columns is None:
                        headers = {col: str(value).strip() for col, value in values.items() if value}
                        mapped = [MAPPING.get(name, name) for name in headers.values()]
                        missing = REQUIRED.difference(mapped)
                        if missing:
                            raise ValueError(f'工作表「{sheet}」缺少欄位：{", ".join(sorted(missing))}；請選有逐筆車況的工作表')
                        columns = {col: name for col, name in headers.items()
                                   if MAPPING.get(name, name) in FIELDS or name.startswith('event[')}
                        destinations = [MAPPING.get(name, name) for name in columns.values()]
                        if len(destinations) != len(set(destinations)):
                            raise ValueError('工作表有重複或重複對應的欄位名稱')
                        continue
                    record = {columns[col]: value for col, value in values.items()}
                    normalized = {MAPPING.get(name, name): value for name, value in record.items()
                                  if MAPPING.get(name, name) in FIELDS}
                    try:
                        normalized['timestamp'] = timestamp(normalized.get('timestamp'), info['mac_dates'])
                        for name in ('vehicle', 'journey'):
                            normalized[name] = identifier(normalized.get(name))
                        if not normalized.get('event_reports'):
                            reports = {}
                            for name, value in record.items():
                                if not name.startswith('event[') or '].' not in name or value in (None, ''):
                                    continue
                                slot, field = name.split('].', 1)
                                report = reports.setdefault(slot, {})
                                report[field] = timestamp(value, info['mac_dates']) if field in ('startTime', 'endTime') else identifier(value) if field == 'type' else value
                            normalized['event_reports'] = json.dumps([report for report in reports.values() if report.get('type')], ensure_ascii=False)
                        normalized['source_excel_row'] = excel_row
                    except ValueError as error:
                        raise ValueError(f'工作表「{sheet}」第 {excel_row} 列：{error}') from error
                    trip_key = (normalized['vehicle'], normalized['journey'])
                    if trip_key not in trip_index:
                        trip_index[trip_key] = {'vehicle': trip_key[0], 'journey': trip_key[1], 'records': 0,
                                               'start': normalized['timestamp'], 'end': normalized['timestamp'], 'chunks': []}
                    trip = trip_index[trip_key]
                    trip['records'] += 1
                    trip['start'] = min(trip['start'], normalized['timestamp'])
                    trip['end'] = max(trip['end'], normalized['timestamp'])
                    batch.append(normalized)
                    batch_trips.add(trip_key)
                    count += 1
                    if len(batch) >= CHUNK_ROWS:
                        flush()
        flush()
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as error:
        raise ValueError('工作表內容無法讀取，請確認檔案完整且未加密') from error
    if not trip_index:
        raise ValueError('工作表沒有可播放的逐筆資料')
    return {'trips': sorted(trip_index.values(), key=lambda t: (t['vehicle'], t['start'], t['journey'])),
            'chunks': chunks, 'records': count, 'sheet': sheet, 'source_sha256': hashlib.sha256(content).hexdigest()}


def trip_data(data: dict, filename: str, vehicle: str, journey: str, config):
    trip = next((t for t in data['trips'] if (t['vehicle'], t['journey']) == (vehicle, journey)), None)
    if trip is None:
        raise ValueError('找不到所選車輛／行程')
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    writer.writeheader()
    for chunk_id in trip['chunks']:
        reader = csv.DictReader(io.StringIO(gzip.decompress(data['chunks'][chunk_id]).decode('utf-8'), newline=''))
        writer.writerows(row for row in reader if (row['vehicle'], row['journey']) == (vehicle, journey))
    content = buffer.getvalue().encode('utf-8')
    payload, metadata = analyze_uploaded(read_replay_bytes(content), filename, config, hashlib.sha256(content).hexdigest())
    metadata.update(worksheet=data['sheet'], source_sha256=data['source_sha256'])
    return payload, metadata
