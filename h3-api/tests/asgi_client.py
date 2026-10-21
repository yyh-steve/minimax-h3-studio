"""Small synchronous facade over HTTPX's supported ASGI transport for tests."""
import asyncio
import httpx
class TestClient:
    __test__=False
    def __init__(self,app):self.app=app
    def request(self,method,url,**kwargs):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver') as c:
                return await c.request(method,url,**kwargs)
        return asyncio.run(call())
    def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
    def post(self,url,**kwargs):return self.request('POST',url,**kwargs)
    def delete(self,url,**kwargs):return self.request('DELETE',url,**kwargs)
