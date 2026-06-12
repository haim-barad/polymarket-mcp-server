"""CLI entry point: python -m bot.tools.position_analyzer [--telegram]"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bot.position_analyzer import run_analysis, render_markdown_report, write_vault_report, post_to_telegram
from datetime import datetime, timezone
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path("/Users/haimbarad/.hermes/polymarket-bot/state"))
    parser.add_argument("--wallet", type=str, default="0x55183ffA1a169C2bc92d8b5E9B5Aeb444A637023")
    parser.add_argument("--vault-path", type=Path,
                        default=Path("/Users/haimbarad/Library/Mobile Documents/com~apple~CloudDocs/Vaults/Vault-Personal/Finance/US/Polymarket/Position-Analysis.md"))
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()
    
    analyses = run_analysis(args.state_dir, args.wallet)
    as_of = datetime.now(timezone.utc)
    report = render_markdown_report(analyses, as_of)
    
    total_cost = sum(a["cost_basis_usd"] for a in analyses)
    total_value = sum(a["current_value_usd"] for a in analyses)
    print(f"\n{len(analyses)} positions analyzed")
    print(f"  Cost basis: ${total_cost:.2f}")
    print(f"  Current value: ${total_value:.2f}")
    print(f"  Mark-to-market P&L: ${total_value - total_cost:+.2f}")
    
    write_vault_report(report, args.vault_path)
    if args.telegram:
        post_to_telegram(report)
