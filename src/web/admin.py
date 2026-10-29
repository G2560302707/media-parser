import csv
from datetime import datetime, time, timedelta, timezone
import io
import os
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, flash, g, jsonify, redirect, render_template, request, session, url_for

from werkzeug.security import generate_password_hash

from configs.general_constants import DOMAIN_TO_NAME
from src.auth import admin_required, csrf_protected, format_log_time, generate_api_key, hash_api_key
from src.db import get_daily_trend, get_db, get_platform_distribution, get_top_users, set_setting, setting, utcnow
from src.utils.shortcut_builder import (
    clear_shortcut_override,
    normalize_icloud_install_url,
    override_status,
    save_shortcut_override,
)
from src.utils.table_query import paginate_memory_list, query_paginated_table


bp = Blueprint("admin", __name__, url_prefix="/admin")


def _positive_int(value, default=1, maximum=1000):
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _non_negative_int(value, default=0, maximum=9999):
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _positive_page(value):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _parse_shanghai_to_utc_iso(val: str, is_end: bool = False) -> str | None:
    if not val:
        return None
    val = val.strip()
    if not val:
        return None
    if len(val) == 10 and val.count("-") == 2:
        try:
            d = datetime.strptime(val, "%Y-%m-%d").date()
            t = time.max if is_end else time.min
            dt = datetime.combine(d, t, tzinfo=ZoneInfo("Asia/Shanghai"))
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            return None
    clean_val = val.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(clean_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


# ----------------------------------------------------------------------
# 1. 系统概览 (Overview)
# ----------------------------------------------------------------------

@bp.get("", endpoint="dashboard")
@bp.get("/overview", endpoint="overview")
@admin_required
def overview():
    try:
        days = int(request.args.get("days", 7))
        if days not in (7, 30, 90, 180, 0):
            days = 7
    except (TypeError, ValueError):
        days = 7

    db = get_db()
    stats = db.execute(
        "SELECT COUNT(*) calls, SUM(status_code < 400) successes, COUNT(DISTINCT user_id) users "
        "FROM request_logs WHERE datetime(created_at) >= datetime('now','-1 day')"
    ).fetchone()
    settings = {row["key"]: row["value"] for row in db.execute("SELECT * FROM system_settings")}
    chart_data = get_daily_trend(user_id=None, days=days)
    pie_data = get_platform_distribution(user_id=None, days=days)
    top_users = get_top_users(days=days, limit=10)

    return render_template(
        "admin/overview.html",
        stats=stats,
        settings=settings,
        chart_data=chart_data,
        pie_data=pie_data,
        top_users=top_users,
        current_days=days,
        active_nav="overview",
    )


# ----------------------------------------------------------------------
# 2. 系统设置 (Settings)
# ----------------------------------------------------------------------

@bp.get("/settings")
@admin_required
def settings():
    db = get_db()
    settings = {row["key"]: row["value"] for row in db.execute("SELECT * FROM system_settings")}
    return render_template(
        "admin/settings.html",
        settings=settings,
        shortcut_override=override_status(),
        active_nav="settings",
    )


@bp.post("/settings")
@admin_required
@csrf_protected
def update_settings():
    db = get_db()
    for name in ("global_api_enabled", "homepage_enabled", "demo_enabled", "registration_enabled", "api_tip_enabled"):
        set_setting(name, "1" if request.form.get(name) else "0")
    set_setting("default_user_qps", _positive_int(request.form.get("default_user_qps"), 2))

    if request.form.get("chk_unlimited_trial") == "1" or request.form.get("default_trial_days") == "0":
        set_setting("default_trial_days", 0)
    else:
        set_setting("default_trial_days", _non_negative_int(request.form.get("default_trial_days"), 365, 9999))

    if request.form.get("chk_unlimited_credits") == "1" or request.form.get("default_initial_credits") == "-1":
        set_setting("default_initial_credits", -1)
    else:
        try:
            raw_val = request.form.get("default_initial_credits")
            if raw_val is None or str(raw_val).strip() == "":
                init_cred = 100
            else:
                raw_cred = int(str(raw_val).replace(",", "").strip())
                init_cred = -1 if raw_cred == -1 else max(0, min(raw_cred, 99999999))
        except (TypeError, ValueError):
            init_cred = 100
        set_setting("default_initial_credits", init_cred)

    set_setting("api_tip_author", (request.form.get("api_tip_author") or "").strip() or "G2560302707")
    set_setting("api_tip_website", (request.form.get("api_tip_website") or "").strip() or "https://github.com/G2560302707/media-parser/tree/main")
    set_setting("api_tip_notice", (request.form.get("api_tip_notice") or "").strip() or "本接口由开源项目 media-parser 提供服务")

    # 保存平台 Cookie 覆盖凭据及动态代理 API 地址
    cookie_keys_saved = []
    for key in request.form:
        if key.startswith("cookie_") or key == "dynamic_proxy_api_url":
            set_setting(key, (request.form.get(key) or "").strip())
            if key.startswith("cookie_"):
                cookie_keys_saved.append(key)

    db.commit()

    # Cookie 保存即触发本地字段校验，巡检页即时获得最新结论。
    if cookie_keys_saved:
        from src.utils.cookie_registry import COOKIE_REGISTRY, check_cookie_fields, record_probe

        for key in cookie_keys_saved:
            for platform_key, entry in COOKIE_REGISTRY.items():
                if key in entry["db_keys"]:
                    # 元宝/视频号双键共享一条凭据：两键皆空时才视为未配置。
                    combined = "".join(
                        (setting(db_key) or "") for db_key in entry["db_keys"]
                    ).strip()
                    if not combined:
                        record_probe(platform_key, "not_configured", "field_check", "凭据已清空")
                    else:
                        field = check_cookie_fields(platform_key)
                        record_probe(
                            platform_key,
                            "healthy" if field["status"] == "ok" else field["status"],
                            "field_check",
                            field["detail"],
                        )
                    break

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")
    if is_ajax:
        return jsonify({"succ": True, "message": "系统设置已自动保存"})

    flash("系统设置已保存", "success")
    return redirect(url_for("admin.settings"))


@bp.post("/shortcut")
@admin_required
@csrf_protected
def update_shortcut():
    """Replace the iPhone shortcut install file or iCloud link without redeploying."""
    action = (request.form.get("action") or "save").strip()
    settings_url = url_for("admin.settings") + "#tab=access"

    if action == "restore":
        clear_shortcut_override()
        flash("已恢复为程序内置生成的快捷指令文件，立即生效", "success")
        return redirect(settings_url)

    try:
        icloud_url = normalize_icloud_install_url(request.form.get("shortcut_icloud_url"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(settings_url)

    uploaded = request.files.get("shortcut_file")
    file_bytes = uploaded.read() if uploaded and uploaded.filename else b""
    if file_bytes:
        error = save_shortcut_override(file_bytes)
        if error:
            flash(error, "error")
            return redirect(settings_url)

    set_setting("shortcut_icloud_url", icloud_url)
    get_db().commit()

    if file_bytes:
        flash("快捷指令安装文件已替换，用户下次下载就是新文件，不用重新发版", "success")
    elif icloud_url:
        flash("已改用 iCloud 分享链接安装，立即生效", "success")
    else:
        flash("已清除 iCloud 链接，用户将下载本站安装文件", "success")
    return redirect(settings_url)


# ----------------------------------------------------------------------
# 2.5 Cookie 凭据巡检 (Cookie Inspection)
# ----------------------------------------------------------------------

def _overview_payload():
    from src.utils.cookie_registry import build_finding, build_overview

    overview = build_overview()
    overview["finding"] = build_finding(overview)
    return overview


@bp.get("/cookies")
@admin_required
def cookies():
    return render_template(
        "admin/cookies.html",
        overview=_overview_payload(),
        active_nav="cookies",
    )


@bp.get("/api/cookies/overview")
@admin_required
def cookies_overview_api():
    return jsonify({"succ": True, "data": _overview_payload()})


@bp.post("/api/cookies/field-check")
@admin_required
@csrf_protected
def cookies_field_check():
    """对单平台重跑本地字段校验（零网络），刷新巡检结论。"""
    from src.utils.cookie_registry import (
        COOKIE_REGISTRY, check_cookie_fields, get_cookie_source, record_probe,
    )

    platform_key = (request.json or {}).get("platform") if request.is_json else request.form.get("platform")
    if platform_key not in COOKIE_REGISTRY:
        return jsonify({"succ": False, "message": "未注册的平台凭据"}), 400

    source = get_cookie_source(platform_key)
    if source == "none":
        record_probe(platform_key, "not_configured", "field_check", "未配置凭据")
        return jsonify({"succ": True, "data": {"status": "not_configured"}})

    field = check_cookie_fields(platform_key)
    record_probe(
        platform_key,
        "healthy" if field["status"] == "ok" else field["status"],
        "field_check",
        field["detail"],
    )
    return jsonify({"succ": True, "data": {"status": field["status"], "missing": field["missing"]}})


@bp.post("/api/cookies/credential")
@admin_required
@csrf_protected
def cookies_credential():
    """巡检页行内保存单平台 Cookie（不经过 settings 全量表单，避免重置无关设置）。"""
    from src.utils.cookie_registry import (
        COOKIE_REGISTRY, check_cookie_fields, get_cookie_source, record_probe,
    )

    platform_key = (request.json or {}).get("platform") if request.is_json else request.form.get("platform")
    value = ((request.json or {}).get("value") if request.is_json else request.form.get("value")) or ""
    value = value.strip()
    entry = COOKIE_REGISTRY.get(platform_key)
    if not entry:
        return jsonify({"succ": False, "message": "未注册的平台凭据"}), 400

    db_key = entry["db_keys"][0]  # 元宝/视频号共享凭据，写入设置页主键即可
    set_setting(db_key, value)
    get_db().commit()

    source = get_cookie_source(platform_key)
    if source == "none":
        record_probe(platform_key, "not_configured", "field_check", "凭据已清空")
        status, missing = "not_configured", []
    else:
        field = check_cookie_fields(platform_key)
        status = "healthy" if field["status"] == "ok" else field["status"]
        missing = field["missing"]
        record_probe(platform_key, status, "field_check", field["detail"])
    return jsonify({"succ": True, "data": {"status": status, "missing": missing}})


@bp.post("/api/cookies/push")
def cookies_push():
    """采集工具免登录推送通道：以 COOKIE_PUSH_TOKEN 代替管理员会话鉴权。

    服务器 .env 未配置 COOKIE_PUSH_TOKEN 时通道开放（任何人可写入凭据），
    公网部署建议配置随机令牌，采集端用 --token 或交互输入传入。
    """
    from src.utils.cookie_registry import (
        COOKIE_REGISTRY, check_cookie_fields, get_cookie_source, record_probe,
    )

    expected = (os.getenv("COOKIE_PUSH_TOKEN") or "").strip()
    if expected:
        supplied = (request.json or {}).get("token") if request.is_json else request.form.get("token")
        if (supplied or "").strip() != expected:
            return jsonify({"succ": False, "message": "推送令牌错误或缺失"}), 401

    platform_key = (request.json or {}).get("platform") if request.is_json else request.form.get("platform")
    value = ((request.json or {}).get("value") if request.is_json else request.form.get("value")) or ""
    value = value.strip()
    entry = COOKIE_REGISTRY.get(platform_key)
    if not entry:
        return jsonify({"succ": False, "message": "未注册的平台凭据"}), 400

    db_key = entry["db_keys"][0]  # 元宝/视频号共享凭据，写入设置页主键即可
    set_setting(db_key, value)
    get_db().commit()

    source = get_cookie_source(platform_key)
    if source == "none":
        record_probe(platform_key, "not_configured", "field_check", "凭据已清空")
        status, missing = "not_configured", []
    else:
        field = check_cookie_fields(platform_key)
        status = "healthy" if field["status"] == "ok" else field["status"]
        missing = field["missing"]
        record_probe(platform_key, status, "field_check", field["detail"])
    return jsonify({"succ": True, "data": {"status": status, "missing": missing}})


@bp.post("/api/cookies/probe")
@admin_required
@csrf_protected
def cookies_probe():
    """单平台金丝雀双跑对照探针（真实出网，含 5 分钟防抖）。"""
    from src.utils.cookie_prober import probe_platform

    platform_key = (request.json or {}).get("platform") if request.is_json else request.form.get("platform")
    if not platform_key:
        return jsonify({"succ": False, "message": "缺少 platform 参数"}), 400
    result = probe_platform(platform_key)
    if result["status"] == "debounced":
        return jsonify({"succ": False, "message": result["detail"]["error"], "data": result}), 429
    return jsonify({"succ": True, "data": result})


@bp.post("/api/cookies/canary")
@admin_required
@csrf_protected
def cookies_canary():
    """设置平台金丝雀链接。"""
    from src.utils.cookie_registry import COOKIE_REGISTRY, record_probe
    from utils.web_fetcher import UrlParser

    platform_key = (request.json or {}).get("platform") if request.is_json else request.form.get("platform")
    url = ((request.json or {}).get("url") if request.is_json else request.form.get("url")) or ""
    url = url.strip()
    if platform_key not in COOKIE_REGISTRY:
        return jsonify({"succ": False, "message": "未注册的平台凭据"}), 400

    entry = COOKIE_REGISTRY[platform_key]
    if not url:
        set_setting(entry["canary_setting"], "")
        get_db().commit()
        return jsonify({"succ": True, "message": "已清空金丝雀，将使用内置默认样例"})
    if not UrlParser.get_platform(url):
        # 容错：从整段分享文案中提取首个 http(s) 链接（如「6.43 复制打开抖音… https://v.douyin.com/xxx/ AGv:/」）
        import re
        extracted = re.search(r"https?://[^\s'\"<>]+", url)
        if not extracted or not UrlParser.get_platform(extracted.group(0)):
            return jsonify({"succ": False, "message": "链接无法识别平台，请粘贴完整的分享链接"}), 400
        url = extracted.group(0)

    set_setting(entry["canary_setting"], url)
    record_probe(platform_key, "healthy", "field_check", "金丝雀链接已更新")
    get_db().commit()
    return jsonify({"succ": True, "message": "金丝雀链接已保存"})


# ----------------------------------------------------------------------
# 3. 客户账号 (Users)
# ----------------------------------------------------------------------

@bp.get("/users")
@admin_required
def users():
    db = get_db()
    users_where, users_params = [], []
    if u_q := request.args.get("users_q", "").strip():
        users_where.append("u.username LIKE ?")
        users_params.append(f"%{u_q}%")
    if u_role := request.args.get("users_role", "").strip():
        users_where.append("u.role = ?")
        users_params.append(u_role)
    if u_active := request.args.get("users_active", "").strip():
        try:
            val = int(u_active)
            users_where.append("u.active = ?")
            users_params.append(val)
        except ValueError:
            pass

    users_allowed_sorts = {
        "id": "u.id",
        "username": "u.username",
        "role": "u.role",
        "key_count": "key_count",
        "created_at": "u.created_at",
        "expires_at": "u.expires_at",
        "qps_limit": "u.qps_limit",
        "credits": "u.credits",
    }
    users_table = query_paginated_table(
        db,
        base_from_sql="users u",
        select_fields="u.*, (SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id) AS key_count",
        allowed_sorts=users_allowed_sorts,
        default_sort="id",
        default_order="desc",
        where_clauses=users_where,
        where_params=users_params,
        request_args=request.args,
        default_page_size=20,
        prefix="users_",
    )

    return render_template(
        "admin/users.html",
        users=users_table["items"],
        users_table=users_table,
        active_nav="users",
    )


@bp.post("/users/<int:user_id>")
@admin_required
@csrf_protected
def update_user(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["role"] == "admin":
        flash("无法操作该用户账号", "error")
        return redirect(url_for("admin.users"))

    # 快捷切换启用/停用状态
    if "active" in request.form and "qps_limit" not in request.form and "credits" not in request.form and "expires_at" not in request.form:
        active_val = 1 if request.form.get("active") in ("1", "true", "on") else 0
        db.execute("UPDATE users SET active=? WHERE id=? AND role!='admin'", (active_val, user_id))
        db.commit()
        status_text = "已启用" if active_val else "已停用"
        flash(f"已成功{status_text}客户「{user['username']}」", "success")
        return redirect(url_for("admin.users"))

    expires = request.form.get("expires_at", "").strip()
    expires_at = None
    if expires:
        try:
            local_end = datetime.combine(datetime.strptime(expires, "%Y-%m-%d").date(), time.max)
            expires_at = local_end.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            flash("到期日期格式无效", "error")
            return redirect(url_for("admin.users"))

    credits_raw = request.form.get("credits", "").replace(",", "").strip()
    try:
        user_credits = int(credits_raw) if credits_raw else None
    except ValueError:
        user_credits = None

    qps = _positive_int(request.form.get("qps_limit"), 2)

    if "active" in request.form:
        active_val = 1 if request.form.get("active") in ("1", "true", "on") else 0
        if user_credits is not None:
            db.execute(
                "UPDATE users SET active=?, expires_at=?, qps_limit=?, credits=? WHERE id=? AND role!='admin'",
                (active_val, expires_at, qps, user_credits, user_id),
            )
        else:
            db.execute(
                "UPDATE users SET active=?, expires_at=?, qps_limit=? WHERE id=? AND role!='admin'",
                (active_val, expires_at, qps, user_id),
            )
    else:
        # 编辑弹窗保存（不修改现有启用/停用状态）
        if user_credits is not None:
            db.execute(
                "UPDATE users SET expires_at=?, qps_limit=?, credits=? WHERE id=? AND role!='admin'",
                (expires_at, qps, user_credits, user_id),
            )
        else:
            db.execute(
                "UPDATE users SET expires_at=?, qps_limit=? WHERE id=? AND role!='admin'",
                (expires_at, qps, user_id),
            )

    db.commit()
    flash("客户设置已保存", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/reset-password")
@admin_required
@csrf_protected
def reset_password(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["role"] == "admin":
        flash("无法操作该用户账号", "error")
        return redirect(url_for("admin.users"))

    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("重置密码至少需要 8 位", "error")
        return redirect(url_for("admin.users"))

    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new_password), user_id),
    )
    db.commit()
    flash(f"已成功重置客户「{user['username']}」的密码", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/batch")
@admin_required
@csrf_protected
def batch_users():
    db = get_db()
    action = request.form.get("action", "").strip()
    select_mode = request.form.get("select_mode", "page").strip()

    if not action:
        flash("未指定批量操作类型", "error")
        return redirect(url_for("admin.users"))

    if select_mode == "all":
        users_where, users_params = [], []
        if u_q := (request.form.get("users_q") or request.form.get("q") or "").strip():
            users_where.append("u.username LIKE ?")
            users_params.append(f"%{u_q}%")
        if u_role := (request.form.get("users_role") or request.form.get("role") or "").strip():
            users_where.append("u.role = ?")
            users_params.append(u_role)
        if u_active := (request.form.get("users_active") or request.form.get("active") or "").strip():
            try:
                val = int(u_active)
                users_where.append("u.active = ?")
                users_params.append(val)
            except ValueError:
                pass
        users_where.append("u.role != 'admin'")

        where_sql = " WHERE " + " AND ".join(users_where)
        subquery = f"SELECT u.id FROM users u{where_sql}"
        matched_count = db.execute(f"SELECT COUNT(*) as c FROM users WHERE id IN ({subquery})", users_params).fetchone()["c"]

        if action == "enable":
            db.execute(f"UPDATE users SET active=1 WHERE id IN ({subquery})", users_params)
            flash(f"已批量启用符合筛选条件的全部 {matched_count} 个客户账号", "success")
        elif action == "disable":
            db.execute(f"UPDATE users SET active=0 WHERE id IN ({subquery})", users_params)
            flash(f"已批量停用符合筛选条件的全部 {matched_count} 个客户账号", "success")
        elif action == "adjust_credits":
            mode = request.form.get("credits_mode", "add")
            try:
                amount_raw = str(request.form.get("credits_amount", 0)).replace(",", "").strip()
                amount = int(amount_raw)
            except ValueError:
                amount = 0

            if mode == "set":
                db.execute(f"UPDATE users SET credits=? WHERE id IN ({subquery})", [amount] + users_params)
            elif mode == "add":
                db.execute(f"UPDATE users SET credits=credits+? WHERE id IN ({subquery}) AND credits!=-1", [amount] + users_params)
            elif mode == "deduct":
                db.execute(f"UPDATE users SET credits=MAX(0, credits-?) WHERE id IN ({subquery}) AND credits!=-1", [amount] + users_params)
            flash(f"已批量更新符合筛选条件的全部 {matched_count} 个客户账号积分", "success")
        elif action == "set_qps":
            try:
                qps = _positive_int(request.form.get("qps_limit"), 2)
                db.execute(f"UPDATE users SET qps_limit=? WHERE id IN ({subquery})", [qps] + users_params)
                flash(f"已批量设置符合筛选条件的全部 {matched_count} 个客户并发 QPS 为 {qps}", "success")
            except ValueError:
                flash("QPS 格式无效", "error")
        elif action == "set_expires":
            expires = request.form.get("expires_at", "").strip()
            expires_at = None
            if expires:
                try:
                    local_end = datetime.combine(datetime.strptime(expires, "%Y-%m-%d").date(), time.max)
                    expires_at = local_end.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).isoformat(timespec="seconds")
                except ValueError:
                    flash("到期日期格式无效", "error")
                    return redirect(url_for("admin.users"))
            db.execute(f"UPDATE users SET expires_at=? WHERE id IN ({subquery})", [expires_at] + users_params)
            flash(f"已批量设置符合筛选条件的全部 {matched_count} 个客户账号到期时间", "success")
        else:
            flash("不支持的批量操作类型", "error")
            return redirect(url_for("admin.users"))

        db.commit()
        return redirect(url_for("admin.users"))

    ids_raw = request.form.get("ids", "").strip()
    if not ids_raw:
        flash("请先勾选需要批量操作的客户账号", "error")
        return redirect(url_for("admin.users"))

    try:
        user_ids = [int(i.strip()) for i in ids_raw.split(",") if i.strip().isdigit()]
    except ValueError:
        user_ids = []

    if not user_ids:
        flash("未获取到有效的用户 ID 列表", "error")
        return redirect(url_for("admin.users"))

    placeholders = ",".join(["?"] * len(user_ids))

    if action == "enable":
        db.execute(f"UPDATE users SET active=1 WHERE id IN ({placeholders}) AND role!='admin'", user_ids)
        flash(f"已批量启用选中的 {len(user_ids)} 个客户账号", "success")
    elif action == "disable":
        db.execute(f"UPDATE users SET active=0 WHERE id IN ({placeholders}) AND role!='admin'", user_ids)
        flash(f"已批量停用选中的 {len(user_ids)} 个客户账号", "success")
    elif action == "adjust_credits":
        mode = request.form.get("credits_mode", "add")
        try:
            amount = int(request.form.get("credits_amount", 0))
        except ValueError:
            amount = 0

        if mode == "set":
            db.execute(f"UPDATE users SET credits=? WHERE id IN ({placeholders}) AND role!='admin'", [amount] + user_ids)
        elif mode == "add":
            db.execute(f"UPDATE users SET credits=credits+? WHERE id IN ({placeholders}) AND role!='admin' AND credits!=-1", [amount] + user_ids)
        elif mode == "deduct":
            db.execute(f"UPDATE users SET credits=MAX(0, credits-?) WHERE id IN ({placeholders}) AND role!='admin' AND credits!=-1", [amount] + user_ids)
        flash(f"已批量更新选中的 {len(user_ids)} 个客户账号积分", "success")
    elif action == "set_qps":
        try:
            qps = _positive_int(request.form.get("qps_limit"), 2)
            db.execute(f"UPDATE users SET qps_limit=? WHERE id IN ({placeholders}) AND role!='admin'", [qps] + user_ids)
            flash(f"已批量设置选中的 {len(user_ids)} 个客户并发 QPS 为 {qps}", "success")
        except ValueError:
            flash("QPS 格式无效", "error")
    elif action == "set_expires":
        expires = request.form.get("expires_at", "").strip()
        expires_at = None
        if expires:
            try:
                local_end = datetime.combine(datetime.strptime(expires, "%Y-%m-%d").date(), time.max)
                expires_at = local_end.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).isoformat(timespec="seconds")
            except ValueError:
                flash("到期日期格式无效", "error")
                return redirect(url_for("admin.users"))
        db.execute(f"UPDATE users SET expires_at=? WHERE id IN ({placeholders}) AND role!='admin'", [expires_at] + user_ids)
        flash(f"已批量设置选中的 {len(user_ids)} 个客户账号到期时间", "success")
    else:
        flash("不支持的批量操作类型", "error")
        return redirect(url_for("admin.users"))

    db.commit()
    return redirect(url_for("admin.users"))


# ----------------------------------------------------------------------
# 4. API 密钥 (Keys)
# ----------------------------------------------------------------------

@bp.get("/keys")
@admin_required
def keys():
    db = get_db()
    keys_where, keys_params = [], []
    if k_q := request.args.get("keys_q", "").strip():
        keys_where.append("(k.name LIKE ? OR u.username LIKE ?)")
        keys_params.extend([f"%{k_q}%", f"%{k_q}%"])
    if k_active := request.args.get("keys_active", "").strip():
        try:
            val = int(k_active)
            keys_where.append("k.active = ?")
            keys_params.append(val)
        except ValueError:
            pass

    keys_allowed_sorts = {
        "id": "k.id",
        "name": "k.name",
        "username": "u.username",
        "created_at": "k.created_at",
        "last_used_at": "k.last_used_at",
        "qps_limit": "k.qps_limit",
        "active": "k.active",
    }
    keys_table = query_paginated_table(
        db,
        base_from_sql="api_keys k JOIN users u ON u.id=k.user_id",
        select_fields="k.*, u.username, u.role user_role, u.qps_limit user_qps",
        allowed_sorts=keys_allowed_sorts,
        default_sort="id",
        default_order="desc",
        where_clauses=keys_where,
        where_params=keys_params,
        request_args=request.args,
        default_page_size=20,
        prefix="keys_",
    )
    all_users = db.execute("SELECT id, username, role, active FROM users ORDER BY id ASC").fetchall()

    return render_template(
        "admin/keys.html",
        api_keys=keys_table["items"],
        keys_table=keys_table,
        all_users=all_users,
        new_api_key=session.pop("new_api_key", None),
        active_nav="keys",
    )


@bp.post("/keys")
@admin_required
@csrf_protected
def create_key():
    name = request.form.get("name", "").strip()[:40] or "管理员专属密钥"
    target_user_id = request.form.get("user_id", "").strip()
    db = get_db()
    if target_user_id:
        user = db.execute("SELECT * FROM users WHERE id=?", (target_user_id,)).fetchone()
        if not user:
            flash("指定的归属账号不存在", "error")
            return redirect(url_for("admin.keys"))
        user_id = user["id"]
        target_name = user["username"]
    else:
        user_id = g.user["id"]
        target_name = g.user["username"]

    qps_raw = request.form.get("qps_limit", "").strip()
    qps = _positive_int(qps_raw) if qps_raw else None

    raw_key = generate_api_key()

    db.execute(
        "INSERT INTO api_keys(user_id, name, key, qps_limit, created_at) VALUES(?,?,?,?,?)",
        (user_id, name, raw_key, qps, utcnow()),
    )
    db.commit()
    session["new_api_key"] = raw_key
    flash(f"已为「{target_name}」成功创建 API 密钥「{name}」", "success")
    return redirect(url_for("admin.keys"))


@bp.post("/keys/<int:key_id>/update")
@admin_required
@csrf_protected
def update_key(key_id):
    db = get_db()
    name = request.form.get("name", "").strip()[:40] if "name" in request.form else None
    qps_raw = request.form.get("qps_limit", "").strip() if "qps_limit" in request.form else None
    qps = _positive_int(qps_raw) if qps_raw else None
    active = 1 if request.form.get("active") else 0

    if name:
        db.execute("UPDATE api_keys SET name=?, active=?, qps_limit=? WHERE id=?", (name, active, qps, key_id))
    else:
        db.execute("UPDATE api_keys SET active=?, qps_limit=? WHERE id=?", (active, qps, key_id))

    db.commit()
    flash("API Key 已成功更新", "success")
    return redirect(url_for("admin.keys"))


@bp.post("/keys/<int:key_id>/delete")
@admin_required
@csrf_protected
def delete_key(key_id):
    db = get_db()
    db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    db.commit()
    flash("API 密钥已彻底删除", "success")
    return redirect(url_for("admin.keys"))


@bp.post("/keys/batch")
@admin_required
@csrf_protected
def batch_keys():
    db = get_db()
    action = request.form.get("action", "").strip()
    select_mode = request.form.get("select_mode", "page").strip()

    if not action:
        flash("未指定批量操作类型", "error")
        return redirect(url_for("admin.keys"))

    if select_mode == "all":
        keys_where, keys_params = [], []
        if k_q := (request.form.get("keys_q") or request.form.get("q") or "").strip():
            keys_where.append("(k.name LIKE ? OR u.username LIKE ?)")
            keys_params.extend([f"%{k_q}%", f"%{k_q}%"])
        if k_user_id := (request.form.get("keys_user_id") or request.form.get("user_id") or "").strip():
            if k_user_id.isdigit():
                keys_where.append("k.user_id = ?")
                keys_params.append(int(k_user_id))
        if k_active := (request.form.get("keys_active") or request.form.get("active") or "").strip():
            try:
                val = int(k_active)
                keys_where.append("k.active = ?")
                keys_params.append(val)
            except ValueError:
                pass

        where_sql = (" WHERE " + " AND ".join(keys_where)) if keys_where else ""
        subquery = f"SELECT k.id FROM api_keys k LEFT JOIN users u ON u.id=k.user_id{where_sql}"
        matched_count = db.execute(f"SELECT COUNT(*) as c FROM api_keys WHERE id IN ({subquery})", keys_params).fetchone()["c"]

        if action == "enable":
            db.execute(f"UPDATE api_keys SET active=1 WHERE id IN ({subquery})", keys_params)
            flash(f"已批量启用符合筛选条件的全部 {matched_count} 个 API Key", "success")
        elif action == "disable":
            db.execute(f"UPDATE api_keys SET active=0 WHERE id IN ({subquery})", keys_params)
            flash(f"已批量停用符合筛选条件的全部 {matched_count} 个 API Key", "success")
        elif action == "set_qps":
            qps_raw = request.form.get("qps_limit", "").strip()
            qps = _positive_int(qps_raw) if qps_raw else None
            db.execute(f"UPDATE api_keys SET qps_limit=? WHERE id IN ({subquery})", [qps] + keys_params)
            flash(f"已批量更新符合筛选条件的全部 {matched_count} 个 API Key 限流", "success")
        elif action == "delete":
            db.execute(f"DELETE FROM api_keys WHERE id IN ({subquery})", keys_params)
            flash(f"已批量彻底删除符合筛选条件的全部 {matched_count} 个 API Key", "success")
        else:
            flash("不支持的批量操作类型", "error")
            return redirect(url_for("admin.keys"))

        db.commit()
        return redirect(url_for("admin.keys"))

    ids_raw = request.form.get("ids", "").strip()
    if not ids_raw:
        flash("请先勾选需要批量操作的 API Key", "error")
        return redirect(url_for("admin.keys"))

    try:
        key_ids = [int(i.strip()) for i in ids_raw.split(",") if i.strip().isdigit()]
    except ValueError:
        key_ids = []

    if not key_ids:
        flash("未获取到有效的密钥 ID 列表", "error")
        return redirect(url_for("admin.keys"))

    placeholders = ",".join(["?"] * len(key_ids))

    if action == "enable":
        db.execute(f"UPDATE api_keys SET active=1 WHERE id IN ({placeholders})", key_ids)
        flash(f"已批量启用选中的 {len(key_ids)} 个 API Key", "success")
    elif action == "disable":
        db.execute(f"UPDATE api_keys SET active=0 WHERE id IN ({placeholders})", key_ids)
        flash(f"已批量停用选中的 {len(key_ids)} 个 API Key", "success")
    elif action == "set_qps":
        qps_raw = request.form.get("qps_limit", "").strip()
        qps = _positive_int(qps_raw) if qps_raw else None
        db.execute(f"UPDATE api_keys SET qps_limit=? WHERE id IN ({placeholders})", [qps] + key_ids)
        flash(f"已批量更新选中的 {len(key_ids)} 个 API Key 限流", "success")
    elif action == "delete":
        db.execute(f"DELETE FROM api_keys WHERE id IN ({placeholders})", key_ids)
        flash(f"已批量彻底删除选中的 {len(key_ids)} 个 API Key", "success")
    else:
        flash("不支持的批量操作类型", "error")
        return redirect(url_for("admin.keys"))

    db.commit()
    return redirect(url_for("admin.keys"))


# ----------------------------------------------------------------------
# 5. 支持平台 (Platforms)
# ----------------------------------------------------------------------

@bp.get("/platforms")
@admin_required
def platforms():
    db = get_db()
    configured = {row["platform"]: row for row in db.execute("SELECT * FROM platform_settings")}

    # 平台与域名映射
    platform_to_domains = {}
    for domain, pname in DOMAIN_TO_NAME.items():
        platform_to_domains.setdefault(pname, []).append(domain)
    for pname in platform_to_domains:
        platform_to_domains[pname].sort()

    # 近 24 小时各平台解析调用统计
    stats_rows = db.execute(
        "SELECT platform, COUNT(*) as calls, SUM(status_code < 400) as successes, AVG(duration_ms) as avg_duration "
        "FROM request_logs "
        "WHERE datetime(created_at) >= datetime('now', '-1 day') "
        "GROUP BY platform"
    ).fetchall()
    stats_by_platform = {}
    for r in stats_rows:
        p = r["platform"]
        calls = r["calls"] or 0
        successes = r["successes"] or 0
        avg_dur = round(r["avg_duration"]) if r["avg_duration"] else 0
        stats_by_platform[p] = {
            "calls": calls,
            "successes": successes,
            "avg_duration": avg_dur,
            "success_rate": round(successes * 100.0 / calls, 1) if calls > 0 else 0,
        }

    platforms_list = []
    for name in sorted(set(DOMAIN_TO_NAME.values())):
        row = configured.get(name)
        p_stats = stats_by_platform.get(name, {"calls": 0, "successes": 0, "avg_duration": 0, "success_rate": 0})
        platforms_list.append({
            "name": name,
            "enabled": True if row is None else bool(row["enabled"]),
            "qps_limit": None if row is None else row["qps_limit"],
            "domains": platform_to_domains.get(name, []),
            "domain_count": len(platform_to_domains.get(name, [])),
            "calls_24h": p_stats["calls"],
            "successes_24h": p_stats["successes"],
            "avg_duration_24h": p_stats["avg_duration"],
            "success_rate_24h": p_stats["success_rate"],
        })

    # 顶部统计概览
    total_count = len(platforms_list)
    enabled_count = sum(1 for p in platforms_list if p["enabled"])
    disabled_count = total_count - enabled_count
    limited_count = sum(1 for p in platforms_list if p["qps_limit"])
    total_calls_24h = sum(p["calls_24h"] for p in platforms_list)
    total_successes_24h = sum(p["successes_24h"] for p in platforms_list)
    overall_success_rate = round(total_successes_24h * 100.0 / total_calls_24h, 1) if total_calls_24h > 0 else 100.0

    summary_stats = {
        "total_count": total_count,
        "enabled_count": enabled_count,
        "disabled_count": disabled_count,
        "limited_count": limited_count,
        "total_calls_24h": total_calls_24h,
        "overall_success_rate": overall_success_rate,
    }

    # 过滤条件筛选
    p_q = request.args.get("platforms_q", "").strip().lower()
    p_status = request.args.get("platforms_status", "").strip()

    filtered_platforms = []
    for p in platforms_list:
        if p_q:
            match_name = p_q in p["name"].lower()
            match_domains = any(p_q in d.lower() for d in p["domains"])
            if not (match_name or match_domains):
                continue
        if p_status == "enabled" and not p["enabled"]:
            continue
        elif p_status == "disabled" and p["enabled"]:
            continue
        elif p_status == "limited" and not p["qps_limit"]:
            continue
        filtered_platforms.append(p)

    platforms_allowed_sorts = {
        "name": "name",
        "domains": "domain_count",
        "calls": "calls_24h",
        "rate": "success_rate_24h",
        "success_rate": "success_rate_24h",
        "qps": lambda x: (x["qps_limit"] is None, x["qps_limit"] or 0),
        "status": lambda x: (not x["enabled"], x["name"]),
    }

    platforms_table = paginate_memory_list(
        filtered_platforms,
        allowed_sorts=platforms_allowed_sorts,
        default_sort="name",
        default_order="asc",
        request_args=request.args,
        default_page_size=50,
        prefix="platforms_",
    )

    return render_template(
        "admin/platforms.html",
        platforms=platforms_table["items"],
        platforms_table=platforms_table,
        summary_stats=summary_stats,
        active_nav="platforms",
    )


@bp.post("/platforms/<path:platform>")
@admin_required
@csrf_protected
def update_platform(platform):
    if platform not in set(DOMAIN_TO_NAME.values()):
        flash("未知平台", "error")
        return redirect(url_for("admin.platforms"))
    qps_raw = request.form.get("qps_limit", "").strip()
    qps = _positive_int(qps_raw) if qps_raw else None
    db = get_db()
    db.execute(
        "INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,?,?) "
        "ON CONFLICT(platform) DO UPDATE SET enabled=excluded.enabled,qps_limit=excluded.qps_limit",
        (platform, 1 if request.form.get("enabled") else 0, qps),
    )
    db.commit()
    flash(f"{platform} 设置已保存", "success")
    return redirect(url_for("admin.platforms"))


@bp.post("/platforms/batch")
@admin_required
@csrf_protected
def batch_platforms():
    db = get_db()
    action = request.form.get("action", "").strip()
    select_mode = request.form.get("select_mode", "page").strip()

    if not action:
        flash("未指定批量操作类型", "error")
        return redirect(url_for("admin.platforms"))

    if select_mode == "all":
        configured = {row["platform"]: row for row in db.execute("SELECT * FROM platform_settings")}
        platform_to_domains = {}
        for domain, pname in DOMAIN_TO_NAME.items():
            platform_to_domains.setdefault(pname, []).append(domain)

        platforms_list = []
        for name in sorted(set(DOMAIN_TO_NAME.values())):
            row = configured.get(name)
            enabled = True if row is None else bool(row["enabled"])
            qps_limit = None if row is None else row["qps_limit"]
            platforms_list.append({
                "name": name,
                "enabled": enabled,
                "qps_limit": qps_limit,
                "domains": platform_to_domains.get(name, []),
            })

        p_q = (request.form.get("platforms_q") or request.form.get("q") or "").strip().lower()
        p_status = (request.form.get("platforms_status") or request.form.get("status") or "").strip()

        filtered_platforms = []
        for p in platforms_list:
            if p_q:
                match_name = p_q in p["name"].lower()
                match_domains = any(p_q in d.lower() for d in p["domains"])
                if not (match_name or match_domains):
                    continue
            if p_status == "enabled" and not p["enabled"]:
                continue
            elif p_status == "disabled" and p["enabled"]:
                continue
            elif p_status == "limited" and not p["qps_limit"]:
                continue
            filtered_platforms.append(p)

        platform_names = [p["name"] for p in filtered_platforms]
        if not platform_names:
            flash("未找到符合筛选条件的支持平台", "error")
            return redirect(url_for("admin.platforms"))

        qps_raw = request.form.get("qps_limit", "").strip()
        qps = _positive_int(qps_raw) if qps_raw else None

        for name in platform_names:
            if action == "enable":
                db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=1", (name,))
            elif action == "disable":
                db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,0,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=0", (name,))
            elif action == "set_qps":
                db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,?) ON CONFLICT(platform) DO UPDATE SET qps_limit=excluded.qps_limit", (name, qps))

        db.commit()
        flash(f"已成功批量更新符合筛选条件的全部 {len(platform_names)} 个支持平台配置", "success")
        return redirect(url_for("admin.platforms"))

    names_raw = request.form.get("names", "").strip()
    if not names_raw:
        flash("请先勾选需要批量操作的支持平台", "error")
        return redirect(url_for("admin.platforms"))

    platform_names = [n.strip() for n in names_raw.split(",") if n.strip() in set(DOMAIN_TO_NAME.values())]

    if not platform_names:
        flash("未获取到有效的支持平台列表", "error")
        return redirect(url_for("admin.platforms"))

    qps_raw = request.form.get("qps_limit", "").strip()
    qps = _positive_int(qps_raw) if qps_raw else None

    for name in platform_names:
        if action == "enable":
            db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=1", (name,))
        elif action == "disable":
            db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,0,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=0", (name,))
        elif action == "set_qps":
            db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,?) ON CONFLICT(platform) DO UPDATE SET qps_limit=excluded.qps_limit", (name, qps))

    db.commit()
    flash(f"已成功批量更新 {len(platform_names)} 个支持平台配置", "success")
    return redirect(url_for("admin.platforms"))


# ----------------------------------------------------------------------
# 6. 开发者文档 (Docs)
# ----------------------------------------------------------------------

@bp.get("/docs")
@admin_required
def docs():
    db = get_db()
    my_keys = db.execute(
        "SELECT * FROM api_keys WHERE user_id=? ORDER BY id DESC", (g.user["id"],)
    ).fetchall()
    return render_template(
        "admin/docs.html",
        my_nav="docs",
        my_keys=my_keys,
        active_nav="docs",
    )


# ----------------------------------------------------------------------
# 7. 运行日志 (Logs)
# ----------------------------------------------------------------------

@bp.get("/logs")
@admin_required
def logs():
    db = get_db()
    logs_where, logs_params = [], []
    if l_q := request.args.get("logs_q", request.args.get("q", "")).strip():
        logs_where.append("(l.input_url LIKE ? OR u.username LIKE ? OR l.error_code LIKE ?)")
        logs_params.extend([f"%{l_q}%", f"%{l_q}%", f"%{l_q}%"])
    if l_status := request.args.get("logs_status", request.args.get("status_code", "")).strip():
        if l_status == "200":
            logs_where.append("l.status_code < 400")
        elif l_status == "error":
            logs_where.append("l.status_code >= 400")
        elif l_status.isdigit():
            logs_where.append("l.status_code = ?")
            logs_params.append(int(l_status))
    if l_platform := request.args.get("logs_platform", request.args.get("platform", "")).strip():
        logs_where.append("l.platform = ?")
        logs_params.append(l_platform)
    if utc_start := _parse_shanghai_to_utc_iso(request.args.get("logs_start_date", request.args.get("start_date", "")), is_end=False):
        logs_where.append("l.created_at >= ?")
        logs_params.append(utc_start)
    if utc_end := _parse_shanghai_to_utc_iso(request.args.get("logs_end_date", request.args.get("end_date", "")), is_end=True):
        logs_where.append("l.created_at <= ?")
        logs_params.append(utc_end)

    logs_allowed_sorts = {
        "id": "l.id",
        "created_at": "l.created_at",
        "duration_ms": "l.duration_ms",
        "status_code": "l.status_code",
        "username": "u.username",
        "platform": "l.platform",
    }

    req_args = dict(request.args)
    if "page" in req_args and "logs_page" not in req_args:
        req_args["logs_page"] = req_args["page"]

    logs_table = query_paginated_table(
        db,
        base_from_sql="request_logs l LEFT JOIN users u ON u.id=l.user_id LEFT JOIN api_keys k ON k.id=l.api_key_id",
        select_fields="l.*, u.username, k.key",
        allowed_sorts=logs_allowed_sorts,
        default_sort="id",
        default_order="desc",
        where_clauses=logs_where,
        where_params=logs_params,
        request_args=req_args,
        prefix="logs_",
    )

    configured = {row["platform"]: row for row in db.execute("SELECT * FROM platform_settings")}
    platforms_list = []
    for name in sorted(set(DOMAIN_TO_NAME.values())):
        row = configured.get(name)
        platforms_list.append({
            "name": name,
            "enabled": True if row is None else bool(row["enabled"]),
            "qps_limit": None if row is None else row["qps_limit"],
        })

    return render_template(
        "admin/logs.html",
        logs=logs_table["items"],
        logs_table=logs_table,
        platforms=platforms_list,
        active_nav="logs",
    )


def _safe_csv_cell(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


@bp.get("/logs/export.csv")
@admin_required
def export_logs():
    db = get_db()
    export_type = request.args.get("export_type", "all")
    ids_raw = request.args.get("ids", "").strip()

    where_clauses, params = [], []

    if export_type == "selected" and ids_raw:
        try:
            id_list = [int(i.strip()) for i in ids_raw.split(",") if i.strip().isdigit()]
            if id_list:
                placeholders = ",".join(["?"] * len(id_list))
                where_clauses.append(f"l.id IN ({placeholders})")
                params.extend(id_list)
        except ValueError:
            pass
    else:
        if l_q := request.args.get("logs_q", request.args.get("q", "")).strip():
            where_clauses.append("(l.input_url LIKE ? OR u.username LIKE ? OR l.error_code LIKE ?)")
            params.extend([f"%{l_q}%", f"%{l_q}%", f"%{l_q}%"])
        if l_status := request.args.get("logs_status", request.args.get("status_code", "")).strip():
            if l_status == "200":
                where_clauses.append("l.status_code < 400")
            elif l_status == "error":
                where_clauses.append("l.status_code >= 400")
            elif l_status.isdigit():
                where_clauses.append("l.status_code = ?")
                params.append(int(l_status))
        if l_platform := request.args.get("logs_platform", request.args.get("platform", "")).strip():
            where_clauses.append("l.platform = ?")
            params.append(l_platform)
        if utc_start := _parse_shanghai_to_utc_iso(request.args.get("logs_start_date", request.args.get("start_date", "")), is_end=False):
            where_clauses.append("l.created_at >= ?")
            params.append(utc_start)
        if utc_end := _parse_shanghai_to_utc_iso(request.args.get("logs_end_date", request.args.get("end_date", "")), is_end=True):
            where_clauses.append("l.created_at <= ?")
            params.append(utc_end)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(("时间", "客户", "Key", "平台", "请求路径", "请求 URL", "状态码", "耗时（毫秒）", "错误码"))

    cursor = db.execute(
        f"SELECT l.*, u.username, k.key FROM request_logs l "
        f"LEFT JOIN users u ON u.id=l.user_id LEFT JOIN api_keys k ON k.id=l.api_key_id "
        f"{where_sql} ORDER BY l.id DESC",
        params,
    )
    while rows := cursor.fetchmany(1000):
        for row in rows:
            writer.writerow((
                _safe_csv_cell(format_log_time(row["created_at"])),
                _safe_csv_cell(row["username"] or "在线体验"),
                _safe_csv_cell(f"{row['key'][:11]}••••••••" if row["key"] else ""),
                _safe_csv_cell(row["platform"] or ""),
                _safe_csv_cell(row["path"]),
                _safe_csv_cell(row["input_url"] or ""),
                _safe_csv_cell(row["status_code"]),
                _safe_csv_cell(row["duration_ms"]),
                _safe_csv_cell(row["error_code"] or ""),
            ))

    csv_bytes = output.getvalue().encode("utf-8")
    filename = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("request-logs-%Y%m%d-%H%M%S.csv")
    return Response(
        csv_bytes,
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@bp.post("/logs/<int:log_id>/delete")
@admin_required
@csrf_protected
def delete_log(log_id):
    db = get_db()
    db.execute("DELETE FROM request_logs WHERE id=?", (log_id,))
    db.commit()
    flash(f"已删除日志记录 #{log_id}", "success")
    return redirect(url_for("admin.logs"))


@bp.post("/logs/batch")
@admin_required
@csrf_protected
def batch_logs():
    db = get_db()
    action = request.form.get("action", "").strip()
    select_mode = request.form.get("select_mode", "page").strip()

    if not action:
        flash("未指定批量操作类型", "error")
        return redirect(url_for("admin.logs"))

    if select_mode == "all":
        where_clauses, params = [], []
        if l_q := (request.form.get("logs_q") or request.form.get("q") or "").strip():
            where_clauses.append("(l.input_url LIKE ? OR u.username LIKE ? OR l.error_code LIKE ?)")
            params.extend([f"%{l_q}%", f"%{l_q}%", f"%{l_q}%"])
        if l_status := (request.form.get("logs_status") or request.form.get("status_code") or "").strip():
            if l_status == "200":
                where_clauses.append("l.status_code < 400")
            elif l_status == "error":
                where_clauses.append("l.status_code >= 400")
            elif l_status.isdigit():
                where_clauses.append("l.status_code = ?")
                params.append(int(l_status))
        if l_platform := (request.form.get("logs_platform") or request.form.get("platform") or "").strip():
            where_clauses.append("l.platform = ?")
            params.append(l_platform)
        if utc_start := _parse_shanghai_to_utc_iso(request.form.get("logs_start_date") or request.form.get("start_date") or "", is_end=False):
            where_clauses.append("l.created_at >= ?")
            params.append(utc_start)
        if utc_end := _parse_shanghai_to_utc_iso(request.form.get("logs_end_date") or request.form.get("end_date") or "", is_end=True):
            where_clauses.append("l.created_at <= ?")
            params.append(utc_end)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        subquery = f"SELECT l.id FROM request_logs l LEFT JOIN users u ON u.id=l.user_id LEFT JOIN api_keys k ON k.id=l.api_key_id{where_sql}"
        matched_count = db.execute(f"SELECT COUNT(*) as c FROM request_logs WHERE id IN ({subquery})", params).fetchone()["c"]

        if action == "delete":
            db.execute(f"DELETE FROM request_logs WHERE id IN ({subquery})", params)
            db.commit()
            flash(f"已批量删除符合筛选条件的全部 {matched_count} 条日志记录", "success")
        else:
            flash("不支持的批量操作类型", "error")

        return redirect(url_for("admin.logs"))

    ids_raw = request.form.get("ids", "").strip()
    if not ids_raw:
        flash("请先勾选需要批量操作的日志条目", "error")
        return redirect(url_for("admin.logs"))

    try:
        log_ids = [int(i.strip()) for i in ids_raw.split(",") if i.strip().isdigit()]
    except ValueError:
        log_ids = []

    if not log_ids:
        flash("未获取到有效的日志 ID 列表", "error")
        return redirect(url_for("admin.logs"))

    placeholders = ",".join(["?"] * len(log_ids))

    if action == "delete":
        db.execute(f"DELETE FROM request_logs WHERE id IN ({placeholders})", log_ids)
        flash(f"已批量删除选中的 {len(log_ids)} 条日志记录", "success")
    else:
        flash("不支持的批量操作类型", "error")

    db.commit()
    return redirect(url_for("admin.logs"))
