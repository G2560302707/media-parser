from urllib.parse import quote

from flask import Blueprint, Response, redirect, render_template, request, session, url_for
from src.auth import api_base_url
from src.db import get_db, setting
from src.utils.shortcut_builder import load_shortcut_bytes, override_status

bp = Blueprint('web', __name__)


@bp.route('/')
def index():
    """前台展示页面（Landing Page）"""
    if setting('homepage_enabled', '1') != '1':
        user_id = session.get('user_id')
        if user_id:
            user = get_db().execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
            if user:
                if user['role'] == 'admin':
                    return redirect(url_for('admin.overview'))
                return redirect(url_for('portal.overview'))
        return redirect(url_for('auth.login'))

    api_enabled = setting('global_api_enabled', '1') == '1'
    demo_enabled = setting('demo_enabled', '1') == '1'
    return render_template(
        'landing.html',
        api_enabled=api_enabled,
        demo_enabled=demo_enabled,
        parser_enabled=api_enabled and demo_enabled,
        registration_enabled=setting('registration_enabled', '1') == '1',
        logged_in=bool(session.get('user_id')),
    )


@bp.route('/shortcut')
def shortcut_guide():
    """iPhone 快捷指令安装与密钥说明，未登录可访问。"""
    icloud_url = (setting('shortcut_icloud_url') or '').strip()
    uploaded = override_status()['present']
    if icloud_url:
        source = 'icloud'
        install_url = icloud_url
    elif uploaded:
        source = 'uploaded'
        install_url = url_for('web.shortcut_file')
    else:
        source = 'generated'
        install_url = url_for('web.shortcut_file')
    return render_template(
        'shortcut.html',
        registration_enabled=setting('registration_enabled', '1') == '1',
        logged_in=bool(session.get('user_id')),
        shortcut_source=source,
        shortcut_install_url=install_url,
    )


@bp.route('/s')
def shortcut_run():
    """快捷指令打开的轻量解析页。密钥存在浏览器本地，不经过这条 URL。"""
    return render_template(
        'shortcut_run.html',
        share_text=(request.args.get('text') or request.args.get('url') or '')[:2000],
        registration_enabled=setting('registration_enabled', '1') == '1',
    )


@bp.route('/shortcut.shortcut')
def shortcut_file():
    payload = load_shortcut_bytes(api_base_url())
    return Response(
        payload,
        mimetype='application/octet-stream',
        headers={
            'Content-Disposition': (
                'attachment; filename="media-parser.shortcut"; '
                "filename*=UTF-8''" + quote('媒体解析.shortcut')
            ),
            'Cache-Control': 'no-store',
        },
    )
