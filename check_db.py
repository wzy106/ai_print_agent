import sqlite3

# 连接数据库（注意路径，如果在项目根目录直接写文件名就行）
conn = sqlite3.connect("print_logs.db")
conn.row_factory = sqlite3.Row

# 执行查询
cursor = conn.execute("""
    SELECT file_name, source, color, sides, copies, inferred, status 
    FROM print_logs 
    ORDER BY id DESC LIMIT 1
""")

# 打印结果
for row in cursor:
    print(dict(row))

conn.close()