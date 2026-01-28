from helpers import get_tenant_by_phone
import asyncio

async def test():
    tenant = await get_tenant_by_phone('+48732126459')
    if tenant:
        print(f"transfer_enabled: {tenant.get('transfer_enabled')}")
        print(f"transfer_number: {tenant.get('transfer_number')}")
        print(f"booking_enabled: {tenant.get('booking_enabled')}")
    else:
        print('Tenant not found')

asyncio.run(test())