import requests
import json

def get_exchange_rates(base_currency="INR"):
    """
    Fetches latest rates from open access endpoint.
    """
    try:
        url = f"https://open.er-api.com/v6/latest/{base_currency.upper()}"
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get("result") == "success":
            return data.get("rates", {})
    except Exception as e:
        print(f"Currency fetch error: {e}")
    return {base_currency.upper(): 1.0}

def convert_amount(amount, from_curr, to_curr, rates_snapshot=None):
    if from_curr.upper() == to_curr.upper():
        return amount
        
    if rates_snapshot:
        try:
            rates = json.loads(rates_snapshot) if isinstance(rates_snapshot, str) else rates_snapshot
            # If base of snapshot matches from_curr
            if to_curr.upper() in rates:
                return amount * rates[to_curr.upper()]
        except Exception:
            pass
            
    # Fallback to live rates fetch
    rates = get_exchange_rates(from_curr)
    rate = rates.get(to_curr.upper(), 1.0)
    return amount * rate