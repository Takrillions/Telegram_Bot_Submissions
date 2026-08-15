import csv
from io import BytesIO, StringIO
from typing import Any
from openpyxl import Workbook

FORMULA_PREFIXES = ("=", "+", "-", "@")
SHEETS = ("\u041e\u0431\u0437\u043e\u0440", "\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f", "\u041e\u0442\u0432\u0435\u0442\u044b", "\u0410\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u044c", "\u0422\u043e\u043f \u043f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u0446", "\u0410\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u044b")
def safe_cell(v: Any) -> Any:
    if v is None: return ""
    return "'" + v if isinstance(v,str) and v.startswith(FORMULA_PREFIXES) else v
def csv_export(snapshot: dict[str,object]) -> bytes:
    stats=snapshot['statistics']; admins=snapshot['administrators']; f=StringIO(newline=''); w=csv.writer(f); w.writerow(['section','metric','value'])
    for k in ('channel_id','period','conversation_metrics_complete'): w.writerow(['metadata',k,safe_cell(snapshot[k])])
    for k,v in snapshot.get('metadata',{}).items(): w.writerow(['metadata',k,safe_cell(v)])
    for k,v in stats.items():
        if k not in {'media','messages_by_hour','messages_by_weekday','top_subscribers'}: w.writerow(['overview',k,safe_cell(v)])
    for k,v in stats['media'].items(): w.writerow(['media',k,v])
    for r in stats['top_subscribers']: w.writerow(['top_subscriber',safe_cell(r['display_name']),r['message_count']])
    for k in ('tracked_conversation_count','handled_conversation_count','unanswered_conversation_count'):
        w.writerow(['administration',k,safe_cell(admins.get(k,''))])
    for r in admins['admins']:
        for k,v in r.items():
            if k != 'admin_id': w.writerow(['administrator',safe_cell(r['display_name'])+':'+k,safe_cell(v)])
    return ('\ufeff'+f.getvalue()).encode('utf-8')
def xlsx_export(snapshot: dict[str,object]) -> bytes:
    stats=snapshot['statistics']; admins=snapshot['administrators']; wb=Workbook(); wb.remove(wb.active)
    def add(name, rows):
        ws=wb.create_sheet(name)
        for row in rows: ws.append([safe_cell(x) for x in row])
        for col in ws.columns: ws.column_dimensions[col[0].column_letter].width=min(45,max(12,max(len(str(c.value or '')) for c in col)+2))
    add(SHEETS[0], [['metric','value']]+[[k,v] for k,v in snapshot.get('metadata',{}).items()]+[[k,v] for k,v in stats.items() if k not in {'media','messages_by_hour','messages_by_weekday','top_subscribers'}])
    add(SHEETS[1], [['type','count']]+list(stats['media'].items()))
    add(SHEETS[2], [['metric','value']]+[[k,stats.get(k,'')] for k in ('conversation_count','answered_conversation_count','answered_conversation_share','average_first_response_seconds','median_first_response_seconds','conversation_metrics_complete')])
    add(SHEETS[3], [['hour','messages']]+list(stats['messages_by_hour'].items())+[ ['weekday','messages'] ]+list(stats['messages_by_weekday'].items()))
    add(SHEETS[4], [['subscriber','messages']]+[[r['display_name'],r['message_count']] for r in stats['top_subscribers']])
    fields=('display_name','reply_count','unique_conversations_replied','handled_conversations','first_response_count','average_first_response_seconds','median_first_response_seconds','moderation_actions','restrictions','warnings','spam_marks','management_actions')
    team_rows = [
        ['team_metric', 'value'],
        ['tracked_conversation_count', admins.get('tracked_conversation_count', '')],
        ['handled_conversation_count', admins.get('handled_conversation_count', '')],
        ['unanswered_conversation_count', admins.get('unanswered_conversation_count', '')],
    ]
    add(SHEETS[5], team_rows + [[]] + [list(fields)]+[[r.get(k,'') for k in fields] for r in admins['admins']])
    out=BytesIO(); wb.save(out); return out.getvalue()
