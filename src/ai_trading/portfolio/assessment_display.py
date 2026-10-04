"""Assessmentの人間向け表示。JSON契約やdomain計算から分離する。"""
from decimal import Decimal, localcontext
from .models import ARITHMETIC

LABELS = {"take_profit_threshold": "設定した利益閾値に到達",
          "loss_warning": "損失警戒閾値に到達", "daily_move_warning": "日次変動閾値に到達",
          "concentration_warning": "構成比の上限を超過"}
STATES = {"fresh": "有効", "stale": "古い価格・評価不可", "missing": "価格なし・評価不可",
          "blocked": "検証上の停止・評価不可"}


def display_assessment(data):
    """欠測は評価不可と表示し、値がないこととゼロを区別する。"""
    print(f"評価時点: {data['as_of']}")
    if not data["assessments"]:
        print("現在の保有銘柄はありません。")
    with localcontext(ARITHMETIC):
        for row in data["assessments"]:
            metrics, metadata = row["metrics"], row["market_snapshot"]
            price = format(Decimal(metrics["current_price"]), ",f") if metrics["current_price"] is not None else "評価不可"
            pnl = format(Decimal(metrics["unrealized_pnl_pct"])*100, "+.2f")+"%" if metrics["unrealized_pnl_pct"] is not None else "評価不可"
            conditions = " / ".join(LABELS[f] for f in row["assessment_flags"]) or "到達した条件なし"
            print(f"\n{row['symbol']} ({row['currency']})")
            print(f"価格: {price} {row['currency']} / 含み損益率: {pnl}")
            print(f"条件: {conditions}")
            if any(r.get("evaluation") == "not_evaluable" for r in row["reasons"]):
                print("一部の条件はデータ不足により評価できません。")
            print(f"市場データ: {STATES[row['market_data_status']]} / 重要度: {row['severity']}")
            if metadata["data_date"]:
                print(f"日足終値（リアルタイムではありません）: {metadata['data_date']}")
            if metadata["source"]:
                print(f"価格時点: {metadata['as_of']} / 出所: {metadata['source']}")
        for group in data["portfolio"]["currencies"]:
            value = group["market_value"] if group["market_value"] is not None else "評価不可"
            print(f"\n{group['currency']}: 保有 {group['number_of_positions']}銘柄 / 評価額 {value} / 実現損益 {group['realized_pnl']}")
