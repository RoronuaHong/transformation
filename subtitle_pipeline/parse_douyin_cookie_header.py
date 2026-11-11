r"""Parse a captured Douyin request Cookie header into Netscape cookies.douyin.txt.

The cookie header was captured from the logged-in chrome-devtools MCP browser's
request to www.douyin.com/aweme/v1/web/user/profile/self/ via get_network_request
(request headers -> Cookie). We cannot read Chrome's Cookies DB (exclusive lock),
so this captured header is the source of truth.
"""
from __future__ import annotations

from pathlib import Path

# Raw Cookie header value captured from the live logged-in Douyin request.
COOKIE_HEADER = r"""enter_pc_once=1; UIFID_TEMP=0da0dfd58e057446ac94b6955d170d229e63295035f63d277e795c5f35a7022b85d382487c642e7417017929d316e82f03a69b26c94894f110c25a8963377de641046d700cf520ff616d6c81a93f41ea; x-web-secsdk-uid=684affd8-f7ca-4795-b774-34f9094c60df; hevc_supported=true; douyin.com; bd_ticket_guard_core_version=My4zLjM0LWJldGEuMTA%3D; device_web_cpu_core=16; device_web_memory_size=32; architecture=amd64; is_support_rtm_web_ts=1; dy_swidth=2560; dy_sheight=1440; strategyABtestKey=%221794368403.74%22; fpk1=U2FsdGVkX19hk55Vtyxfan/Eqs3BwZI/byyIo+dcePc5K44Xc4CJWtxeBnAQcjyxE9X50I5SVxUyJj53tu4Ocg==; fpk2=5d0dd425008d7328cff9cce489fb55f7; s_v_web_id=verify_mwbkm9vn_pTuoKg5K_D0On_4M5d_AYnr_gaa22flQFWas; passport_csrf_token=3f3fbf7596aeed3a44ce82da2b44bd31; passport_csrf_token_default=3f3fbf7596aeed3a44ce82da2b44bd31; bd_ticket_guard_regenerate_keys_time=2026-11-11/11:40:30; bd_ticket_guard_client_web_domain=2; is_dash_user=1; ttwid=1%7CltFUY8b5cY4AlaVHBYHUJgAGgvY4FyjVhjW1neoljNs%7C1794368457%7C3d36a51563d97df9cc4ff92e4e5a6bedf79400c5ffcdd0540a68ba51851e8683; download_guide=%221%2F20261111%2F0%22; sdk_source_info=7e276470716a68645a606960273f276364697660272927676c715a6d6069756077273f276364697660272927666d776a68605a607d71606b766c6a6b5a7666776c7571273f275e58272927666a6b766a69605a696c6061273f27636469766027292762696a6764695a7364776c6467696076273f275e592772606761776c7360775927582729277672715a646971273f2763646976602729277f6b5a666475273f2763646976602729276d6a6e5a6b6a716c273f2763646976602729276c6b6f5a7f6367273f27636469766027292771273f273332353c3c35333236313c3234272927676c715a75776a716a666a69273f2763646976602778; bit_env=vBG2ZPdWkCH7ASMqR73FJQFRHBPQQBElE8eQL3G-nF_jdk3CRlAjLAaw8c1D00hInes9xB33i9wLNSjTlumxdPb4vDlz_MPnl4m3383c0vYFSHut8Ccik3SZ0Vhx2rlNVKCTDzI4dx_Y3ynar2aER6bAILT-nphyHYLxmJiVF3jxX8DjxdBl932qjAvG-fAINHEOPZ-LYyXY9RO7Gi4UtN_VKiFsIanfWK5zj7MSGkOTJbnZT37uvIAho9ca4LElcLEP2vFtdGBvZLtMRssEaDgYgtKZDhlt9frjfXc3cxRG_YGhrVQl5304iD0tb4OyefFvZeY1iJsnho529fT9ZOysP2oY88bUJ9M5rPhZgj5DNuM4rp65uNCb82aUlzTYkUzFmkSxNMo0-oGeIaycYp72edevsBtrI-6JDjgkf_EGU-zm7l-XJypbntR2XZQjLRF28j4h1dp-8hrnvgbbkcCJ0y4wwhIyeKhprid6GwFJHN_5fPd7QKSRhkeKkv6O; gulu_source_res=eyJwX2luIjoiYzgyNGVhNzFkZjQzZjI1MzRkYTRlYmM1ZmI4YTIzZDIyNjA5ZWUzNmRjOThiMDQ4MmUxZDE3YTRhMWMzMDM1NyJ9; passport_auth_mix_state=ow9zqu5fksk914t265pe5dr1xgbt1kc7; passport_mfa_token=CjVjGXuKIkahm2ysP8heXXDLSW1MVLzBm8epaa2QYA9DRBFA6PCwMB1WSrnpWpAg%2BgMhSgnllxpKCjwAAAAAAAAAAAAAUSABohEQAsbcTQWoXJ3PhStpFab%2BRB4Qg6rGNQVscsFN%2FzyLDVxz6XYX%2F%2Ft5d%2BJQreAQtqqhDhj2sdFsIAIiAQNPJ915; d_ticket=6b18b9a2875bff2fb7d742e50c61ab79559e3; biz_trace_id=2844bfd6; passport_assist_user=CjxPaNCasUqgSML0YEC_hTVJ4jG05nvzC-Ks_OncnN1ja6oxpahCA43icq5wgL2XtiH0LifOiTQCz-zynbsaSgo8AAAAAAAAAAAAAFEgvZOqTvh4L_xI0ljvpk2KCWbqfVFcTlzl5PKq41GovNcYPJx8FCju_p1zzDhokefYEK-ooQ4Yia_WVCABIgED-ly-9A%3D%3D; n_mh=L-9iYh5Y5gIT3HaffoMedu3zZ9pwUWINpZnLUlvm0lY; sid_guard=30f9e7163bfd8bb3d822508bc0bbee7f%7C1794376195%7C5184000%7CSun%2C+10-Jan-2027+05%3A49%3A55+GMT; uid_tt=8b7426f2935b27908a57283166622082; uid_tt_ss=8b7426f2935b27908a57283166622082; sid_tt=30f9e7163bfd8bb3d822508bc0bbee7f; sessionid=30f9e7163bfd8bb3d822508bc0bbee7f; sessionid_ss=30f9e7163bfd8bb3d822508bc0bbee7f; session_tlb_tag=sttt%7C16%7CMPnnFjv9i7PYIlCLwLvuf_________-jor7fJGHL83ZdwttLPP2I7qgprtljCuDs7Ha5xPYRk10%3D; is_staff_user=false; has_biz_token=false; sid_ucp_v1=1.0.0-KDhjNWFjN2FjNDIyOTc1OTk5NTIyMGU0ZDIwMzlkNzU5MjcxZTc4MWQKHwinso_x8gIQg4TQ1wYY7zEgDDCs88fYBTgHQPQHSAQaAmhsIiAzMGY5ZTcxNjNiZmQ4YmIzZDgyMjUwOGJjMGJiZWU3Zg; ssid_ucp_v1=1.0.0-KDhjNWFjN2FjNDIyOTc1OTk5NTIyMGU0ZDIwMzlkNzU5MjcxZTc4MWQKHwinso_x8gIQg4TQ1wYY7zEgDDCs88fYBTgHQPQHSAQaAmhsIiAzMGY5ZTcxNjNiZmQ4YmIzZDgyMjUwOGJjMGJiZWU3Zg; is_dbsc=false; x_tt_token=0030f9e7163bfd8bb3d822508bc0bbee7f000d99f0cae6d39c31bea97c4d60b44c417ed85f76c04351a7bab424a675781bcd86fd02e4509a455460e9433236dc70177472b28bbbecc0ccdc8a739d81cae45edf243f5dd9383d80835c9f5d22e99e521d0edd6bfcdb9e68270c75cd9942409f58ec8c0671f5daa1fcd46b11f8b8a14a9--0a490a20e945a367d6bf05bf13cc4dae04c1c9e36fbd71cdf0ada98271cf5117bfa4ce3e1220f45d81e30a10fc164b96bf53d3e4f0ef21e7265772bfdbc82c88b73b1ac7c26b18f6b4d309-3.0.4; bd_ticket_guard_web_domain=2; login_time=1794376186866; __ac_nonce=06af40205004251bcd310; publish_badge_show_info=%220%2C0%2C0%2C1794376188354%22; DiscoverFeedExposedAd=%7B%7D; __ac_signature=_02B4Z6wo00f01xAm.dAAAIDAT7P2HNkTYxMQBtlAAK8Id8; web_sign_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ2IjoxLCJpZCI6Ijc3MDY3NTM1MTk0MDgxMjEzOTUiLCJkIjowLCJzIjoiIiwidHMiOjE3OTQzNzYxOTl9.bxoIDlAUVb8KRJyfdZB26_X2O-XrPM8SVpRXZfxCHS8; UIFID=0da0dfd58e057446ac94b6955d170d229e63295035f63d277e795c5f35a7022bf9187d6e28186016cd2fdf112112495b759a211298b21b3a3b72daa71c5247e382d0eceacf20a21057f650c63caf91081e93498921e79e063092c979db8f32d887692ddda226a1e022220aaffa03f683d188978ce187e1661ec96283a11114fb0e093f9073aa808793172127cbc3d4ee8c00f7010a68e6fea52d28bc053b851d; home_can_add_dy_2_desktop=%220%22; stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A2560%2C%5C%22screen_height%5C%22%3A1440%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A16%2C%5C%22device_memory%5C%22%3A32%2C%5C%22downlink%5C%22%3A1.3%2C%5C%22effective_type%5C%22%3A%5C%223g%5C%22%2C%5C%22round_trip_time%5C%22%3A1150%7D%22; _bd_ticket_crypt_cookie=ea6f849eb2880411a32c6f0012e9abd9; __security_mc_1_s_sdk_sign_data_key_web_protect=c9e612ea-4e3d-9f8f; __security_mc_1_s_sdk_cert_key=2f99601c-489f-a4e0; __security_mc_1_s_sdk_crypt_sdk=591e25a7-4c12-a1da; __security_server_data_status=1; bd-web-sign-page-guard=eyJ0c19zaWduIjoidHMuMi5jOTU2OTc5NTVkNzcyY2MwZjFiYTFlMzRjOGE0ZTVkNGMwODljNmNkOWM0OGY5MmQ3ZDhhZjU5NzY0ODlhMjlhYzRmYmU4N2QyMzE5Y2YwNTMxODYyNGNlZGExNDkxMWNhNDA2ZGVkYmVkZGIyZTEwZmNlOGQ0ZjYxNDAyNTc1ZCIsIndlYl92ZXJzaW9uIjoiMiIsInJlZV9wdWJsaWNfa2V5IjoiQk5MYnNVQU1JMXVFU1hMZnlVVFE1WkloZFBOUExENTBOOWhUa1RyVC9lQ0tRZjVyR200Zkwwa3pkblZXZ1ltd012Y2t3eTQ2d3o5eUlWakowR2VYbDZFPSIsIndlYl9zaWduX3R5cGUiOiIxIiwicmVxX2NvbnRlbnQiOiJ0aWNrZXQsZG9tYWluLHRpbWVzdGFtcCIsInJlcV9zaWduIjoiS2U1OVBsWVYzTDNnT3pLNDNLUnNrcTc1cW9XMGd2NDF5OG9hNkFsMW1KTT0iLCJ0aW1lc3RhbXAiOjE3OTQzNzYyMTUsInRfdHJ1c3QiOjF9; bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6MiwiYmQtdGlja2V0LWd1YXJkLWl0ZXJhdGlvbi12ZXJzaW9uIjoxLCJiZC10aWNrZXQtZ3VhcmQtcmVlLXB1YmxpYy1rZXkiOiJCTkxic1VBTUkxdUVTWExmeVVUUTVaSWhkUE5QTEQ1ME45aFRrVHJUL2VDS1FmNXJHbTRmTDBremRuVldnWW13TXZja3d5NDZ3ejl5SVZqSjBHZVhsNkU9IiwiYmQtdGlja2V0LWd1YXJkLXdlYi12ZXJzaW9uIjoyfQ%3D%3D; wkzyjzsbl=8; SelfTabRedDotControl=%5B%5D; FOLLOW_LIVE_POINT_INFO=%22MS4wLjABAAAAlmMtM861g6VmKMVX9ucn5aFNGjT0hPoMeHybs1y1Iwg%2F1794412800000%2F0%2F1794376224650%2F0%22; bd_ticket_guide_tool=1; IsDouyinActive=true; odin_tt=58edefd6d3ba2fcb2d62e8314086dc04f8ac992e0832f5d8ed45a27e6f98d732df2506d19f6b25193bbaf1ad296d3abe2047436571228ad4b66dbff54bc3b7c9"""

OUT = Path(__file__).resolve().parents[0] / "cookies.douyin.txt"
_LOGIN_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "uid_tt_ss"}


def parse(header: str):
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        pairs.append((name, value))
    return pairs


def main() -> None:
    pairs = parse(COOKIE_HEADER)
    rows = ["# Netscape HTTP Cookie File (captured from live logged-in Douyin request)"]
    for name, value in pairs:
        rows.append(
            "\t".join([".douyin.com", "TRUE", "/", "TRUE", "0", name, value])
        )
    OUT.write_text("\n".join(rows) + "\n", encoding="utf-8")
    names = {n for n, _ in pairs}
    present = sorted(names & _LOGIN_NAMES)
    print(f"wrote {len(pairs)} douyin cookies -> {OUT}")
    print("logged_in:", bool(present))
    print("login cookies:", present)
    for n in ("sessionid", "sid_tt", "uid_tt"):
        v = dict(pairs).get(n, "")
        print(f"  {n} = {v[:24]}...")


if __name__ == "__main__":
    main()
