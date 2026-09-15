"""
CNINFO (深证信) ESG数据API客户端
认证方式: mcode = Base64(当前Unix时间戳秒数)
"""
import base64
import time
import logging
from typing import Optional

import requests

from config import CNINFO_CONFIG, CNINFO_ENDPOINTS

logger = logging.getLogger(__name__)

class CNINFOClient:
    """CNINFO数据服务平台客户端"""

    def __init__(self, timeout: int = 15):
        self.base_url = CNINFO_CONFIG['base_url']
        self.token = CNINFO_CONFIG['token']
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': CNINFO_CONFIG['user_agent'],
            'Referer': CNINFO_CONFIG['referer'],
        })

    # ---------------------------------------------------------------
    # mcode 生成（Base64(当前Unix秒级时间戳)）
    # ---------------------------------------------------------------
    @staticmethod
    def _gen_mcode() -> str:
        ts = str(int(time.time()))
        return base64.b64encode(ts.encode()).decode()

    def _get_headers(self) -> dict:
        headers = {'mcode': self._gen_mcode()}
        if self.token:
            headers['token'] = self.token
        return headers

    # ---------------------------------------------------------------
    # 通用请求
    # ---------------------------------------------------------------
    def _request(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        method: str = 'GET',
    ) -> dict:
        url = f'{self.base_url}{endpoint}'
        headers = self._get_headers()
        params = params or {'@limit': '10', 'format': 'json'}

        if method == 'GET':
            resp = self.session.get(
                url, headers=headers, params=params, timeout=self.timeout
            )
        else:
            resp = self.session.post(
                url, headers=headers, data=params, timeout=self.timeout
            )

        if resp.status_code != 200:
            raise RuntimeError(
                f'API请求失败 [{resp.status_code}]: {resp.text[:200]}'
            )

        data = resp.json()
        code = data.get('resultcode', 0)
        if code != 0:
            logger.warning(f'API返回异常 code={code}: {data.get("resultmsg", "")}')
            raise PermissionError(
                f'API权限不足 (code={code}): {data.get("resultmsg", "")}'
            )

        return data

    # ---------------------------------------------------------------
    # ESG数据 API 接口
    # ---------------------------------------------------------------
    def fetch_governance(self, params: Optional[dict] = None) -> dict:
        """公司治理议题"""
        return self._request(CNINFO_ENDPOINTS['governance'], params)

    def fetch_environment(self, params: Optional[dict] = None) -> dict:
        """环境议题"""
        return self._request(CNINFO_ENDPOINTS['environment'], params)

    def fetch_social(self, params: Optional[dict] = None) -> dict:
        """社会议题"""
        return self._request(CNINFO_ENDPOINTS['social'], params)

    def fetch_factor_score(self, params: Optional[dict] = None) -> dict:
        """因子库评分"""
        return self._request(CNINFO_ENDPOINTS['factor_score'], params)

    def fetch_dynamic(self, params: Optional[dict] = None) -> dict:
        """动态数据"""
        return self._request(CNINFO_ENDPOINTS['dynamic'], params)


# ---------------------------------------------------------------
# CLI测试入口
# ---------------------------------------------------------------
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    client = CNINFOClient()
    try:
        for name in ['governance', 'environment', 'social']:
            print(f'\n--- 测试 {name} ---')
            data = getattr(client, f'fetch_{name}')({'@limit': '3'})
            print(f'成功: {data}')
    except PermissionError as e:
        print(f'\n⚠️ 需要CNINFO商业订阅token才能访问ESG数据。')
        print(f'   错误: {e}')
        print(f'\n   获取方式: 联系 wanglibin@cninfo.com.cn 申请数据服务订阅')
        print(f'   订阅后设置环境变量: export CNINFO_TOKEN=your_token')
    except Exception as e:
        print(f'\n❌ 出错: {e}')
