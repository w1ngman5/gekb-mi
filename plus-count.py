import sqlite3
import datetime
db = sqlite3.connect('bot_database.db')
t = datetime.datetime.utcnow().strftime('%Y-%m-%d')
db.execute('INSERT OR IGNORE INTO daily_quota_tracker (date_str, request_count) VALUES (?, 0)', (t,))
db.execute('UPDATE daily_quota_tracker SET request_count = request_count + 150 WHERE date_str = ?', (t,))
db.commit()
print('Успешно обновлено!')
