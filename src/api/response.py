from flask import jsonify
from src.db import setting


def make_response(retcode, retdesc, data, succ, error_code=None):
    """生成统一响应；失败时可提供供调用方判断的稳定错误码。"""
    response = {
        'retcode': retcode,
        'retdesc': retdesc,
        'succ': succ,
        'data': data
    }
    if error_code:
        response['error_code'] = error_code

    try:
        if setting('api_tip_enabled', '1') == '1':
            response['_tip'] = {
                'author': setting('api_tip_author', 'G2560302707'),
                'website': setting('api_tip_website', 'https://github.com/G2560302707/media-parser/tree/main'),
                'notice': setting('api_tip_notice', '本接口由开源项目 media-parser 提供服务')
            }
    except Exception:
        pass

    return jsonify(response)

