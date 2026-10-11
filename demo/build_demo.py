#!/usr/bin/env python3
"""一键构建 DGA demo 数据（全部自编，无任何真实机构数据）。

产出：
  demo/bank_retail.duckdb  模拟银行零售业务库 8 表（含一张"脏"源表供治理演示）
  demo/meta.db             元数据库：数据源/表字段登记/20条标准/15个指标/10条ETL血缘/报送映射

用法：python demo/build_demo.py   （幂等：先删后建）
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dga.config import BUSINESS_DB, META_DB  # noqa: E402
from dga.meta.store import DDL  # noqa: E402

random.seed(42)
TODAY = date(2026, 10, 10)


def build_business_db() -> None:
    import duckdb

    if BUSINESS_DB.exists():
        BUSINESS_DB.unlink()
    conn = duckdb.connect(str(BUSINESS_DB))

    conn.execute("""CREATE TABLE dim_org(org_code VARCHAR, org_name VARCHAR, org_type VARCHAR, parent_org VARCHAR)""")
    orgs = [("010001", "总行营业部", "HEAD", None), ("010101", "黄浦支行", "BRANCH", "010001"),
            ("010102", "静安支行", "BRANCH", "010001"), ("010201", "浦东支行", "BRANCH", "010001"),
            ("010202", "张江支行", "BRANCH", "010001"), ("010301", "徐汇支行", "BRANCH", "010001"),
            ("010401", "虹桥支行", "BRANCH", "010001"), ("010501", "外滩支行", "BRANCH", "010001"),
            ("010601", "陆家嘴支行", "BRANCH", "010001"), ("010701", "自贸区支行", "BRANCH", "010001")]
    conn.executemany("INSERT INTO dim_org VALUES (?,?,?,?)", orgs)

    conn.execute("""CREATE TABLE dim_product(prod_code VARCHAR, prod_name VARCHAR, prod_type VARCHAR, risk_level INTEGER)""")
    prods = [(f"P{i:03d}", n, t, r) for i, (n, t, r) in enumerate([
        ("活期存款", "DEPOSIT", 1), ("定期三个月", "DEPOSIT", 1), ("定期一年", "DEPOSIT", 1),
        ("大额存单", "DEPOSIT", 2), ("结构性存款", "DEPOSIT", 3), ("货币基金", "FUND", 2),
        ("债券基金", "FUND", 3), ("股票基金", "FUND", 5), ("理财产品A", "WEALTH", 3),
        ("理财产品B", "WEALTH", 4), ("贵金属账户", "OTHER", 4), ("外汇买卖", "OTHER", 4)], 1)]
    conn.executemany("INSERT INTO dim_product VALUES (?,?,?,?)", prods)

    # 客户 500：零售 400 / 对公 100
    conn.execute("""CREATE TABLE dim_customer(
        cust_no VARCHAR, cust_name VARCHAR, cert_type VARCHAR, cert_no VARCHAR,
        cust_type VARCHAR, phone VARCHAR, email VARCHAR, addr VARCHAR,
        open_date DATE, status VARCHAR)""")
    custs = []
    for i in range(500):
        ctype = "1" if i < 400 else "2"   # 1=零售 2=对公（STD-D-002）
        certt = random.choice(["1", "2", "3"])  # 身份证/护照/统一社会信用代码
        if certt == "1":   # 居民身份证：6位地址+8位生日+3位顺序+1位校验 = 18位
            cert = (f"3101{random.randint(10,99)}{random.randint(1950,2000)}"
                    f"{random.randint(1,12):02d}{random.randint(1,28):02d}"
                    f"{random.randint(0,999):03d}{random.choice('0123456789X')}")
        elif certt == "2":  # 护照号 P+7位数字
            cert = f"P{random.randint(1000000,9999999)}"
        else:               # 统一社会信用代码：18位
            cert = f"91310000{random.randint(10**7,10**8-1)}X{random.randint(1,9)}"
        name = (f"客户{ i:04d}号" if ctype == "1" else f"上海某某科技{chr(65+i%26)}有限公司")
        custs.append((f"C{i:06d}", name, certt, cert, ctype,
                      f"139{random.randint(10000000,99999999)}",
                      f"user{i:04d}@example.com", f"上海市浦东新区张江路{random.randint(1,999)}号",
                      TODAY - timedelta(days=random.randint(1, 2000)),
                      random.choices(["1", "2", "3"], weights=[85, 10, 5])[0]))
    conn.executemany("INSERT INTO dim_customer VALUES (?,?,?,?,?,?,?,?,?,?)", custs)

    # 账户 1500
    conn.execute("""CREATE TABLE dwd_account(
        acct_no VARCHAR, cust_no VARCHAR, prod_code VARCHAR, org_code VARCHAR,
        open_date DATE, balance DECIMAL(18,2), status VARCHAR)""")
    accts = []
    for i in range(1500):
        c = random.choice(custs)
        accts.append((f"A{i:08d}", c[0], random.choice(prods)[0], random.choice(orgs)[0],
                      TODAY - timedelta(days=random.randint(1, 1500)),
                      round(random.expovariate(1 / 80000), 2),
                      random.choices(["1", "2", "3"], weights=[90, 7, 3])[0]))
    conn.executemany("INSERT INTO dwd_account VALUES (?,?,?,?,?,?,?)", accts)

    # 交易 10000
    conn.execute("""CREATE TABLE dwd_trx(
        trx_id VARCHAR, acct_no VARCHAR, cust_no VARCHAR, trx_date DATE,
        amt DECIMAL(18,2), dr_cr VARCHAR, channel VARCHAR, status VARCHAR)""")
    trxs = []
    for i in range(10000):
        a = random.choice(accts)
        trxs.append((f"T{i:010d}", a[0], a[1],
                     TODAY - timedelta(days=random.randint(0, 180)),
                     round(random.expovariate(1 / 5000) + 1, 2),
                     random.choice(["1", "2"]),  # 1=借 2=贷
                     random.choice(["01", "02", "03", "04"]),  # 柜台/网银/手机/ATM
                     random.choices(["1", "2"], weights=[97, 3])[0]))
    conn.executemany("INSERT INTO dwd_trx VALUES (?,?,?,?,?,?,?,?)", trxs)

    # 客户行为 8000（活跃指标原料）
    conn.execute("""CREATE TABLE dwd_cust_event(
        event_id VARCHAR, cust_no VARCHAR, event_date DATE, event_type VARCHAR, channel VARCHAR)""")
    evs = [(f"E{i:010d}", random.choice(custs)[0],
            TODAY - timedelta(days=random.randint(0, 30)),
            random.choice(["LOGIN", "QUERY", "TRANSFER", "BUY"]),
            random.choice(["02", "03"])) for i in range(8000)]
    conn.executemany("INSERT INTO dwd_cust_event VALUES (?,?,?,?,?)", evs)

    # 客户日余额 500×7 天（日均余额指标原料）
    conn.execute("""CREATE TABLE dwm_cust_balance_daily(cust_no VARCHAR, bal_date DATE, avg_balance DECIMAL(18,2))""")
    bals = [(c[0], TODAY - timedelta(days=d), round(random.expovariate(1 / 100000), 2))
            for c in custs for d in range(7)]
    conn.executemany("INSERT INTO dwm_cust_balance_daily VALUES (?,?,?)", bals)

    # ⭐ 脏源表（治理演示主角）：命名混乱/无注释/类型乱/码值超出
    conn.execute("""CREATE TABLE ods_customer_src(
        Cust_No VARCHAR, CustName VARCHAR, CERTYPE VARCHAR, CertNo VARCHAR,
        MobilePhone VARCHAR, EmailAddr VARCHAR, HomeAddress VARCHAR,
        OpenDt VARCHAR, CustSts VARCHAR, Bal VARCHAR, OrganCode VARCHAR, UpdTime VARCHAR)""")
    dirty = []
    for i, c in enumerate(custs[:200]):
        cert_t = c[2] if random.random() > 0.05 else "9"   # 5% 脏码值（超出标准 1-3）
        dirty.append((c[0], c[1], cert_t, c[3],
                      c[5] if random.random() > 0.03 else "139",   # 3% 坏手机号
                      c[6], c[7], str(c[8]), c[9] if random.random() > 0.04 else "9",  # 4% 坏状态
                      str(round(random.uniform(0, 500000), 2)), random.choice(orgs)[0],
                      (TODAY - timedelta(days=random.randint(0, 30))).isoformat()))
    conn.executemany("INSERT INTO ods_customer_src VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", dirty)

    conn.close()
    print(f"[ok] business db -> {BUSINESS_DB}")


# ---------------- 元数据库种子 ----------------

STANDARDS = [
    # id, name, domain, def, std_type, code_values(JSON|None), naming_rule
    ("STD-C-001", "客户号", "客户", "全行唯一的客户标识", "VARCHAR(20)", None, "cust_no"),
    ("STD-C-002", "证件类型", "客户", "客户身份证件类型代码", "CHAR(1)",
     json.dumps([{"v": "1", "m": "居民身份证"}, {"v": "2", "m": "护照"},
                 {"v": "3", "m": "统一社会信用代码"}, {"v": "4", "m": "港澳台通行证"},
                 {"v": "5", "m": "其他"}]), "cert_type"),
    ("STD-C-003", "证件号码", "客户", "按证件类型对应的号码", "CHAR(18)", None, "cert_no"),
    ("STD-C-004", "手机号码", "客户", "境内手机号 11 位数字", "CHAR(11)", None, "phone"),
    ("STD-C-005", "电子邮箱", "客户", "合法邮箱格式", "VARCHAR(64)", None, "email"),
    ("STD-C-006", "联系地址", "客户", "客户登记联系地址", "VARCHAR(128)", None, "addr"),
    ("STD-D-001", "性别代码", "公共", "0未知 1男 2女", "CHAR(1)",
     json.dumps([{"v": "0", "m": "未知"}, {"v": "1", "m": "男"}, {"v": "2", "m": "女"}]), "gender"),
    ("STD-D-002", "客户类型", "客户", "1零售 2对公", "CHAR(1)",
     json.dumps([{"v": "1", "m": "零售"}, {"v": "2", "m": "对公"}]), "cust_type"),
    ("STD-D-003", "客户状态", "客户", "1正常 2冻结 3销户", "CHAR(1)",
     json.dumps([{"v": "1", "m": "正常"}, {"v": "2", "m": "冻结"}, {"v": "3", "m": "销户"}]), "status"),
    ("STD-A-001", "账号", "账户", "全行唯一的账户标识", "VARCHAR(20)", None, "acct_no"),
    ("STD-A-002", "账户状态", "账户", "1正常 2止付 3销户", "CHAR(1)",
     json.dumps([{"v": "1", "m": "正常"}, {"v": "2", "m": "止付"}, {"v": "3", "m": "销户"}]), "status"),
    ("STD-A-003", "币种代码", "账户", "GB/T 2261 系列，默认 156 人民币", "CHAR(3)",
     json.dumps([{"v": "156", "m": "人民币"}, {"v": "840", "m": "美元"}, {"v": "978", "m": "欧元"}]), "currency"),
    ("STD-T-001", "交易日期", "交易", "交易自然日 YYYY-MM-DD", "DATE", None, "trx_date"),
    ("STD-T-002", "交易金额", "交易", "两位小数十进制金额", "DECIMAL(18,2)", None, "amt"),
    ("STD-T-003", "借贷方向", "交易", "1借 2贷", "CHAR(1)",
     json.dumps([{"v": "1", "m": "借"}, {"v": "2", "m": "贷"}]), "dr_cr"),
    ("STD-T-004", "交易渠道", "交易", "01柜台 02网银 03手机 04ATM", "CHAR(2)",
     json.dumps([{"v": "01", "m": "柜台"}, {"v": "02", "m": "网银"},
                 {"v": "03", "m": "手机"}, {"v": "04", "m": "ATM"}]), "channel"),
    ("STD-O-001", "机构号", "机构", "机构编码树节点", "VARCHAR(10)", None, "org_code"),
    ("STD-P-001", "产品代码", "产品", "产品主键", "VARCHAR(10)", None, "prod_code"),
    ("STD-P-002", "风险等级", "产品", "1-5 级", "INTEGER",
     json.dumps([{"v": str(i), "m": f"{i}级"} for i in range(1, 6)]), "risk_level"),
    ("STD-M-001", "更新时间", "公共", "记录最后更新时间戳", "TIMESTAMP", None, "upd_time"),
]

METRICS = [
    # name, biz_text, calc_sql(duckdb), owner, freq, grain
    ("日活跃客户数", "当日有行为事件的去重客户数",
     "SELECT event_date, COUNT(DISTINCT cust_no) FROM dwd_cust_event WHERE event_date = DATE '2026-10-10' GROUP BY event_date",
     "零售部", "日", "日×全行"),
    ("月活跃客户数", "当月有行为事件的去重客户数",
     "SELECT COUNT(DISTINCT cust_no) FROM dwd_cust_event WHERE event_date >= DATE '2026-10-01'",
     "零售部", "月", "月×全行"),
    ("对公客户日均余额", "对公客户近7日日均余额合计",
     "SELECT SUM(avg_balance) FROM dwm_cust_balance_daily b JOIN dim_customer c ON b.cust_no=c.cust_no WHERE c.cust_type='2'",
     "公司部", "日", "日×全行"),
    ("零售客户日均余额", "零售客户近7日日均余额合计",
     "SELECT SUM(avg_balance) FROM dwm_cust_balance_daily b JOIN dim_customer c ON b.cust_no=c.cust_no WHERE c.cust_type='1'",
     "零售部", "日", "日×全行"),
    ("客户总数", "全行有效客户数（状态=正常）",
     "SELECT COUNT(*) FROM dim_customer WHERE status='1'", "数据部", "日", "日×全行"),
    ("当月新增客户数", "本月新开户客户数",
     "SELECT COUNT(*) FROM dim_customer WHERE open_date >= DATE '2026-10-01'", "数据部", "月", "月×全行"),
    ("对公客户数", "对公有效客户数",
     "SELECT COUNT(*) FROM dim_customer WHERE cust_type='2' AND status='1'", "公司部", "日", "日×全行"),
    ("零售客户数", "零售有效客户数",
     "SELECT COUNT(*) FROM dim_customer WHERE cust_type='1' AND status='1'", "零售部", "日", "日×全行"),
    ("全行AUM", "全行账户总余额",
     "SELECT SUM(balance) FROM dwd_account WHERE status='1'", "数据部", "日", "日×全行"),
    ("当期交易总金额", "近180天交易金额合计",
     "SELECT SUM(amt) FROM dwd_trx", "数据部", "日", "日×全行"),
    ("手机渠道交易占比", "手机渠道(03)交易笔数占比",
     "SELECT SUM(CASE WHEN channel='03' THEN 1 ELSE 0 END)*1.0/COUNT(*) FROM dwd_trx", "数据部", "日", "日×全行"),
    ("交易成功率", "成功状态(1)交易占比",
     "SELECT SUM(CASE WHEN status='1' THEN 1 ELSE 0 END)*1.0/COUNT(*) FROM dwd_trx", "数据部", "日", "日×全行"),
    ("户均余额", "有效账户平均余额",
     "SELECT AVG(balance) FROM dwd_account WHERE status='1'", "数据部", "日", "日×全行"),
    ("高净值客户数", "日均余额大于100万的客户数",
     "SELECT COUNT(DISTINCT cust_no) FROM dwm_cust_balance_daily GROUP BY cust_no HAVING SUM(avg_balance)>1000000",
     "零售部", "日", "日×全行"),
    ("潜在流失客户数", "余额大于1万且90天无交易的客户数",
     "SELECT COUNT(DISTINCT a.cust_no) FROM dwd_account a WHERE a.balance>10000 AND a.status='1' "
     "AND a.cust_no NOT IN (SELECT DISTINCT cust_no FROM dwd_trx WHERE trx_date >= DATE '2026-07-13')",
     "零售部", "日", "日×全行"),
]

ETL_SQLS = [
    # name, sql, target_table, schedule
    ("清洗客户源表", "INSERT INTO dim_customer SELECT Cust_No, CustName, CERTYPE, CertNo, '1', MobilePhone, EmailAddr, HomeAddress, CAST(OpenDt AS DATE), CustSts FROM ods_customer_src", "dim_customer", "daily 02:00"),
    ("账户日快照", "INSERT INTO dwm_cust_balance_daily SELECT cust_no, CURRENT_DATE, SUM(balance) FROM dwd_account WHERE status='1' GROUP BY cust_no", "dwm_cust_balance_daily", "daily 03:00"),
    ("对公客户宽表", "CREATE TABLE dm_corp_customer AS SELECT c.cust_no, c.cust_name, SUM(a.balance) AS total_bal FROM dim_customer c JOIN dwd_account a ON c.cust_no=a.cust_no WHERE c.cust_type='2' GROUP BY 1,2", "dm_corp_customer", "daily 04:00"),
    ("客户活跃标记", "CREATE TABLE dm_cust_active AS SELECT cust_no, MAX(event_date) AS last_active FROM dwd_cust_event GROUP BY cust_no", "dm_cust_active", "daily 03:30"),
    ("渠道交易汇总", "CREATE TABLE dm_channel_trx AS SELECT channel, COUNT(*) AS cnt, SUM(amt) AS amt FROM dwd_trx GROUP BY channel", "dm_channel_trx", "daily 05:00"),
    ("高净值名单", "CREATE TABLE dm_hnw AS SELECT cust_no, SUM(avg_balance) AS bal FROM dwm_cust_balance_daily GROUP BY cust_no HAVING SUM(avg_balance)>1000000", "dm_hnw", "daily 05:30"),
    ("零售存款余额", "CREATE TABLE dm_retail_deposit AS SELECT c.cust_no, a.balance FROM dim_customer c JOIN dwd_account a ON c.cust_no=a.cust_no WHERE c.cust_type='1'", "dm_retail_deposit", "daily 04:30"),
    ("客户信息报送宽表", "CREATE TABLE rpt_cust_info AS SELECT Cust_No AS cust_no, CustName AS cust_name, CERTYPE AS cert_type, CertNo AS cert_no, MobilePhone AS phone, HomeAddress AS addr FROM ods_customer_src", "rpt_cust_info", "daily 06:00"),
    ("账户信息报送宽表", "CREATE TABLE rpt_acct_info AS SELECT a.acct_no, a.cust_no, a.balance, a.status FROM dwd_account a WHERE a.status='1'", "rpt_acct_info", "daily 06:00"),
    ("交易失败明细", "CREATE TABLE dm_trx_failed AS SELECT * FROM dwd_trx WHERE status<>'1'", "dm_trx_failed", "daily 05:00"),
]

# ods_customer_src 字段登记（治理演示主角）：12 字段，3 个已贯标
ODS_COLS = [
    # col_name, col_type, comment, nullable, is_pii, compliance_id
    ("Cust_No", "VARCHAR", "客户号", 0, 0, "STD-C-001"),
    ("CustName", "VARCHAR", None, 1, 0, None),
    ("CERTYPE", "VARCHAR", None, 1, 0, "STD-C-002"),
    ("CertNo", "VARCHAR", None, 1, 1, None),
    ("MobilePhone", "VARCHAR", None, 1, 1, None),
    ("EmailAddr", "VARCHAR", None, 1, 0, None),
    ("HomeAddress", "VARCHAR", None, 1, 0, "STD-C-006"),
    ("OpenDt", "VARCHAR", None, 1, 0, None),
    ("CustSts", "VARCHAR", None, 1, 0, None),
    ("Bal", "VARCHAR", None, 1, 0, None),
    ("OrganCode", "VARCHAR", None, 1, 0, None),
    ("UpdTime", "VARCHAR", None, 1, 0, None),
]

DIM_CUSTOMER_COLS = [
    ("cust_no", "VARCHAR", "客户号", 0, 0, "STD-C-001"),
    ("cust_name", "VARCHAR", "客户名称", 1, 0, None),
    ("cert_type", "VARCHAR", "证件类型", 1, 0, "STD-C-002"),
    ("cert_no", "VARCHAR", "证件号码", 1, 1, "STD-C-003"),
    ("cust_type", "VARCHAR", "客户类型 1零售2对公", 1, 0, "STD-D-002"),
    ("phone", "VARCHAR", "手机号", 1, 1, "STD-C-004"),
    ("email", "VARCHAR", "邮箱", 1, 0, "STD-C-005"),
    ("addr", "VARCHAR", "联系地址", 1, 0, "STD-C-006"),
    ("open_date", "DATE", "开户日期", 1, 0, "STD-T-001"),
    ("status", "VARCHAR", "客户状态", 1, 0, "STD-D-003"),
]

DWD_TRX_COLS = [
    ("trx_id", "VARCHAR", "交易流水号", 0, 0, None),
    ("acct_no", "VARCHAR", "账号", 0, 0, "STD-A-001"),
    ("cust_no", "VARCHAR", "客户号", 0, 0, "STD-C-001"),
    ("trx_date", "DATE", "交易日期", 0, 0, "STD-T-001"),
    ("amt", "DECIMAL(18,2)", "交易金额", 0, 0, "STD-T-002"),
    ("dr_cr", "VARCHAR", "借贷方向", 0, 0, "STD-T-003"),
    ("channel", "VARCHAR", "交易渠道", 0, 0, "STD-T-004"),
    ("status", "VARCHAR", "交易状态", 0, 0, None),
]

# 客户信息报送表映射（故意留缺口：email 缺映射；cert_type 有映射但源码值含 '9' 超标准）
REPORT_MAPPING = [
    ("RPT_CUST_INFO", "cust_no", "ods_customer_src", "Cust_No", None),
    ("RPT_CUST_INFO", "cust_name", "ods_customer_src", "CustName", None),
    ("RPT_CUST_INFO", "cert_type", "ods_customer_src", "CERTYPE", None),
    ("RPT_CUST_INFO", "cert_no", "ods_customer_src", "CertNo", None),
    ("RPT_CUST_INFO", "phone", "ods_customer_src", "MobilePhone", None),
    ("RPT_CUST_INFO", "addr", "ods_customer_src", "HomeAddress", None),
    ("RPT_CUST_INFO", "open_date", "ods_customer_src", "OpenDt", "CAST AS DATE"),
    ("RPT_CUST_INFO", "status", "ods_customer_src", "CustSts", None),
    ("RPT_CUST_INFO", "email", None, None, None),  # 规范必填但未映射
]


def build_meta_db() -> None:
    if META_DB.exists():
        META_DB.unlink()
    conn = sqlite3.connect(META_DB)
    conn.executescript(DDL)

    conn.execute("INSERT INTO datasource(name,url,type,created_at) VALUES (?,?,?,?)",
                 ("bank_retail", str(BUSINESS_DB), "duckdb", datetime.now().isoformat()))
    ds_id = 1

    def reg_table(table: str, row_cnt: int, comment: str, cols: list) -> None:
        conn.execute("INSERT INTO meta_table(ds_id,db_name,table_name,row_cnt,comment,pii_level,snapshot_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (ds_id, "main", table, row_cnt, comment, None, datetime.now().isoformat()))
        tid = conn.execute("SELECT id FROM meta_table WHERE table_name=?", (table,)).fetchone()[0]
        for name, ctype, cmt, nullable, pii, std in cols:
            conn.execute("INSERT INTO meta_column(table_id,col_name,col_type,comment,nullable,is_pii,compliance_id) "
                         "VALUES (?,?,?,?,?,?,?)", (tid, name, ctype, cmt, nullable, pii, std))

    reg_table("ods_customer_src", 200, "客户源表（模拟脏数据）", ODS_COLS)
    reg_table("dim_customer", 500, "客户维表", DIM_CUSTOMER_COLS)
    reg_table("dwd_trx", 10000, "交易事实表", DWD_TRX_COLS)

    conn.executemany("INSERT INTO gov_standard(id,name,domain,def_text,std_type,code_values,naming_rule,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     [(s[0], s[1], s[2], s[3], s[4], s[5], s[6], datetime.now().isoformat())
                      for s in STANDARDS])

    conn.executemany("INSERT INTO metric_def(name,biz_text,calc_sql,owner,freq,grain,registered_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     [(m[0], m[1], m[2], m[3], m[4], m[5], datetime.now().isoformat()) for m in METRICS])

    conn.executemany("INSERT INTO etl_sql(name,sql_text,target_table,schedule,updated_at) VALUES (?,?,?,?,?)",
                     [(e[0], e[1], e[2], e[3], datetime.now().isoformat()) for e in ETL_SQLS])

    conn.executemany("INSERT INTO report_mapping(report_table,report_field,src_table,src_field,transform) "
                     "VALUES (?,?,?,?,?)", REPORT_MAPPING)

    conn.commit()
    conn.close()
    print(f"[ok] meta db     -> {META_DB}")
    print(f"     standards={len(STANDARDS)} metrics={len(METRICS)} etl={len(ETL_SQLS)} mapping={len(REPORT_MAPPING)}")


def build_bigdata_env() -> None:
    """Kafka/HDFS/ZK demo 环境种子（幂等）。"""
    from dga.tools.bigdata_tools import HDFS_ROOT, init_bigdata_env

    init_bigdata_env()
    (HDFS_ROOT / "user/dga/data").mkdir(parents=True, exist_ok=True)
    (HDFS_ROOT / "user/dga/data/README").write_text(
        "DGA demo HDFS（本地模拟）\n/user/dga/data  原始数据落盘\n/user/dga/report 报送文件\n/user/dga/tmp    临时对账\n", encoding="utf-8")
    # 从业务库导一份 CSV 到 HDFS（演示 hdfs_cat 真内容）——不依赖 pandas，手动拼
    import duckdb as _d
    cur = _d.connect(str(BUSINESS_DB), read_only=True).execute("SELECT * FROM ods_customer_src LIMIT 20")
    cols = [d[0] for d in cur.description]
    lines = [",".join(cols)] + [",".join(repr(v) if v is not None else "" for v in row) for row in cur.fetchall()]
    (HDFS_ROOT / "user/dga/data/ods_customer_src_head.csv").write_text("\n".join(lines), encoding="utf-8")
    print(f"[ok] bigdata env -> kafka/zk seed + {HDFS_ROOT}")


if __name__ == "__main__":
    build_business_db()
    build_meta_db()
    build_bigdata_env()
    print("demo ready. try: duckdb demo/bank_retail.duckdb 'SELECT COUNT(*) FROM ods_customer_src'")
