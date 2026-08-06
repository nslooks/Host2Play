import os
import sys
import time
import random
import html
import requests
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta
from xvfbwrapper import Xvfb
from DrissionPage import ChromiumPage, ChromiumOptions

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    pass

# ==============================================================================
# 配置区域
# ==============================================================================
RENEW_URLS = [
    "https://host2play.gratis/server/renew?i=447a235e-8a9c-4238-bf9b-b21af6792029",
    # 添加更多链接
]

MAX_CAPTCHA = 3
MAX_RENEW_RETRIES_PER_URL = 20

# ==============================================================================
# 自定义异常
# ==============================================================================
class CaptchaBlocked(Exception):
    pass

# ==============================================================================
# 统一日志
# ==============================================================================
def log(msg, level="INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{prefix} {msg}", flush=True)

# ==============================================================================
# Telegram 通知
# ==============================================================================
def send_tg_photo(token, chat_id, photo_path, caption, parse_mode='HTML'):
    if not token or not chat_id:
        log("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知。", "WARN")
        return
    if not photo_path or not os.path.exists(photo_path):
        log("未找到截图文件，跳过通知。", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as photo_file:
            response = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
                files={"photo": photo_file},
                timeout=30,
            )
        response.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 图片通知异常: {e}", "ERROR")

# ==============================================================================
# 页面元素提取
# ==============================================================================
def get_server_name(page):
    try:
        ele = page.ele('#serverName', timeout=2)
        if ele:
            return ele.text.strip()
    except Exception:
        pass
    return "未知"

def get_expire_time(page):
    try:
        ele = page.ele('#expireDate', timeout=2)
        if ele:
            return ele.text.strip()
    except Exception:
        pass
    # 回退：源版的方式
    selectors = ['text:Expires in:', 'text:Deletes on:']
    for selector in selectors:
        try:
            ele = page.ele(selector, timeout=1)
            if ele:
                text = (ele.text or "").strip()
                if ":" in text:
                    return text.split(":", 1)[1].strip()
                if text:
                    return text
        except Exception:
            pass
    return "未知"

# ==============================================================================
# 构建通知
# ==============================================================================
def build_notification(success, url, server_name, old_expire, new_expire=None, failure_reason=""):
    if success:
        lines = [
            "✅ 续订成功",
            "",
            f"服务器：{server_name}",
            f"到期: {old_expire} -> {new_expire}",
            f"URL: {url}",
        ]
    else:
        lines = [
            "❌ 续订失败",
            "",
            f"服务器：{server_name}",
            f"URL: {url}",
        ]
        if failure_reason:
            lines.append(f"失败原因: {failure_reason}")
    lines.append("")
    lines.append("Host2Play Auto Renew")
    return "\n".join(lines)

def capture_page_screenshot(page, file_name):
    try:
        page.get_screenshot(path=file_name)
        return file_name
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return None

# ==============================================================================
# WARP 重连
# ==============================================================================
def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                      check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                          check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            log("删除注册失败（可能未注册）", "WARN")
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"],
                      check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"],
                      check=True, timeout=30, capture_output=True)
        time.sleep(10)
        new_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "ERROR")
        return False

# ==============================================================================
# reCAPTCHA 辅助函数
# ==============================================================================
def find_recaptcha_frame(page, kind):
    try:
        for frame in page.get_frames():
            frame_url = frame.url or ""
            if "recaptcha" in frame_url and kind in frame_url:
                return frame
    except Exception:
        pass
    return None

def is_recaptcha_solved(page):
    try:
        for frame in page.get_frames():
            try:
                token = frame.run_js("return document.querySelector(\"textarea[name='g-recaptcha-response']\")?.value")
                if token and len(token) > 30:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    anchor = find_recaptcha_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js("return document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true'")
            if checked:
                return True
        except Exception:
            pass
    return False

def is_blocked(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        return bool(bframe.run_js("""
            const h = document.querySelector('.rc-doscaptcha-header-text');
            if (h && h.textContent.toLowerCase().includes('try again later')) return true;
            const e = document.querySelector('.rc-audiochallenge-error-message');
            if (e && e.offsetParent !== null) return true;
            return false;
        """))
    except Exception:
        return False

def click_recaptcha_checkbox(page):
    anchor = find_recaptcha_frame(page, "anchor")
    if not anchor:
        for _ in range(120):
            anchor = find_recaptcha_frame(page, "anchor")
            if anchor:
                break
            time.sleep(1)
    if not anchor:
        raise RuntimeError("未找到 reCAPTCHA anchor frame")
    checkbox = anchor.ele('#recaptcha-anchor', timeout=3)
    if not checkbox:
        raise RuntimeError("未找到 reCAPTCHA 复选框")
    page.actions.move_to(checkbox, duration=random.uniform(0.4, 1.0))
    time.sleep(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except Exception:
        checkbox.click(by_js=True)
    time.sleep(3)
    if is_blocked(page):
        raise CaptchaBlocked("点击复选框后检测到 IP 被封锁")

def switch_to_audio(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele('#audio-response', timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    for attempt in range(3):
        try:
            audio_btn = bframe.ele('#recaptcha-audio-button', timeout=3)
            if audio_btn:
                try:
                    audio_btn.click()
                except Exception:
                    audio_btn.click(by_js=True)
                time.sleep(3)
                if is_blocked(page):
                    raise CaptchaBlocked("点击音频按钮后检测到 IP 被封锁")
                input_box = bframe.ele('#audio-response', timeout=1)
                if input_box and input_box.states.is_displayed:
                    return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        try:
            bframe.run_js("""
                const btn = document.querySelector('#recaptcha-audio-button');
                if (btn) btn.click();
            """)
            time.sleep(3)
            if is_blocked(page):
                raise CaptchaBlocked("JS点击音频按钮后检测到 IP 被封锁")
            input_box = bframe.ele('#audio-response', timeout=1)
            if input_box and input_box.states.is_displayed:
                return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        time.sleep(2)
    return False

def is_audio_mode(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele('#audio-response', timeout=1)
        return bool(input_box and input_box.states.is_displayed)
    except Exception:
        return False

def get_audio_url(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = bframe.ele('.rc-audiochallenge-tdownload-link', timeout=1)
            if link:
                href = link.attr('href')
                if href and len(href) > 10:
                    return html.unescape(href)
            link = bframe.ele('.rc-audiochallenge-ndownload-link', timeout=1)
            if link:
                href = link.attr('href')
                if href and len(href) > 10:
                    return html.unescape(href)
            audio = bframe.ele('#audio-source', timeout=1)
            if audio:
                src = audio.attr('src')
                if src and len(src) > 10:
                    return html.unescape(src)
        except Exception:
            pass
        time.sleep(1)
    return None

def reload_challenge(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return
    try:
        reload_btn = bframe.ele('#recaptcha-reload-button', timeout=2)
        if reload_btn:
            try:
                reload_btn.click()
            except Exception:
                reload_btn.click(by_js=True)
            time.sleep(3)
    except Exception:
        pass

def fill_and_verify(page, text):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele('#audio-response', timeout=2)
        if not input_box:
            return False
        input_box.click()
        input_box.clear()
        input_box.input(text)
    except Exception:
        return False
    time.sleep(random.uniform(0.5, 1.5))
    try:
        verify_btn = bframe.ele('#recaptcha-verify-button', timeout=2)
        if verify_btn:
            try:
                verify_btn.click()
            except Exception:
                verify_btn.click(by_js=True)
    except Exception:
        pass
    return True

def download_audio(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.google.com/",
    }
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))
    for audio_url in urls:
        try:
            r = requests.get(audio_url, headers=headers, timeout=30)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except Exception:
            pass
    return None

def recognize_audio(mp3_path):
    try:
        wav_path = mp3_path.replace(".mp3", ".wav")
        AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as src:
            audio_data = recognizer.record(src)
            text = recognizer.recognize_google(audio_data)
        try:
            os.remove(wav_path)
        except Exception:
            pass
        return text
    except Exception:
        return None

def solve_recaptcha(page):
    start = time.time()
    while time.time() - start < 15:
        if find_recaptcha_frame(page, "anchor"):
            break
        time.sleep(1)
    else:
        raise RuntimeError("reCAPTCHA 加载超时")

    dl_fails = 0
    for i in range(MAX_CAPTCHA):
        if is_recaptcha_solved(page):
            return True
        if is_blocked(page):
            raise CaptchaBlocked("IP 被 Google reCAPTCHA 封锁")

        if i == 0:
            click_recaptcha_checkbox(page)
            time.sleep(2)
            if is_recaptcha_solved(page):
                return True

        if not is_audio_mode(page):
            if not switch_to_audio(page):
                time.sleep(3)
                if not switch_to_audio(page):
                    click_recaptcha_checkbox(page)
                    time.sleep(3)
                    continue
            time.sleep(random.uniform(2, 4))

        if is_blocked(page):
            raise CaptchaBlocked("音频模式检测到 IP 被封锁")

        audio_url = get_audio_url(page)
        if not audio_url:
            reload_challenge(page)
            continue

        mp3 = download_audio(audio_url)
        if not mp3:
            dl_fails += 1
            if dl_fails >= 3:
                raise RuntimeError("音频连续下载失败")
            reload_challenge(page)
            time.sleep(random.uniform(3, 6))
            continue
        dl_fails = 0

        text = recognize_audio(mp3)
        try:
            os.remove(mp3)
        except Exception:
            pass
        if not text:
            reload_challenge(page)
            time.sleep(3)
            continue

        log(f"识别结果: [{text}]")
        fill_and_verify(page, text)
        time.sleep(5)
        if is_recaptcha_solved(page):
            return True
        reload_challenge(page)
        time.sleep(random.uniform(2, 4))
    raise RuntimeError("验证码达到最大尝试次数")

# ==============================================================================
# 单个 URL 续期流程（去掉 IP 预检，直接尝试 + 封锁换 IP）
# ==============================================================================
def renew_single_url(url):
    success = False
    server_name = "未知"
    old_expire = "未知"
    new_expire = "未知"
    screenshot_path = None
    failure_reason = ""
    screenshot_dir = "output/screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    vdisplay = Xvfb(width=1280, height=720, colordepth=24)
    vdisplay.start()

    try:
        for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
            log(f"{'='*20} 续期尝试 {attempt}/{MAX_RENEW_RETRIES_PER_URL} {'='*20}")
            page = None
            try:
                co = ChromiumOptions()
                co.set_browser_path('/usr/bin/google-chrome')
                co.set_argument('--no-sandbox')
                co.set_argument('--disable-dev-shm-usage')
                co.set_argument('--disable-gpu')
                co.set_argument('--disable-setuid-sandbox')
                co.set_argument('--disable-software-rasterizer')
                co.set_argument('--disable-extensions')
                co.set_argument('--no-first-run')
                co.set_argument('--no-default-browser-check')
                co.set_argument('--disable-popup-blocking')
                co.set_argument('--window-size=1280,720')
                co.set_argument('--log-level=3')
                co.set_argument('--silent')
                # 关键：每次用独立的用户数据目录，避免残留 cookie/指纹
                user_data_dir = tempfile.mkdtemp()
                co.set_user_data_path(user_data_dir)
                co.auto_port()
                co.headless(False)
                page = ChromiumPage(co)

                # 反指纹注入
                page.add_init_js("""
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        if (parameter === 37445) return 'Intel Inc.';
                        if (parameter === 37446) return 'Intel(R) UHD Graphics 630';
                        return getParameter.apply(this, [parameter]);
                    };
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
                """)

                log(f"访问: {url}")
                page.get(url, retry=3)
                time.sleep(random.uniform(5, 8))

                server_name = get_server_name(page)
                old_expire = get_expire_time(page)
                log(f"服务器: {server_name}, 到期时间: {old_expire}")

                # 清理遮挡广告
                page.run_js("""
                    const cssSelectors = ['ins.adsbygoogle', 'iframe[src*="ads"]', '.modal-backdrop'];
                    cssSelectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });
                """)
                time.sleep(2)
                consent_btn = page.ele('tag:button@@text():Consent', timeout=2)
                if consent_btn:
                    consent_btn.click()
                    time.sleep(3)

                # 关键：积累真实的鼠标轨迹和滚动数据（源版有，新版删了）
                for _ in range(3):
                    scroll_y = random.randint(200, 600)
                    page.scroll.down(scroll_y)
                    time.sleep(random.uniform(0.5, 1.5))
                    page.actions.move(random.randint(100, 800), random.randint(100, 500))
                    time.sleep(random.uniform(0.5, 1.0))
                time.sleep(random.uniform(1.0, 2.0))

                log("打开续期弹窗...")
                renew_btn1 = page.ele('xpath://button[contains(text(), "Renew server")]', timeout=3)
                if renew_btn1:
                    try:
                        renew_btn1.click()
                    except:
                        renew_btn1.click(by_js=True)
                else:
                    page.run_js("document.querySelectorAll('button').forEach(b => {if(b.textContent.includes('Renew server')) b.click();});")
                time.sleep(3)

                for _ in range(8):
                    if page.ele('text:Expires in:', timeout=0.5) or page.ele('text:Deletes on:', timeout=0.5):
                        break
                    time.sleep(1)

                renew_btn2 = page.ele('xpath://button[contains(text(), "Renew server")]', timeout=2)
                if renew_btn2:
                    try:
                        renew_btn2.click()
                    except:
                        renew_btn2.click(by_js=True)
                time.sleep(random.uniform(7, 10))

                # reCAPTCHA 破解
                anchor_frame = find_recaptcha_frame(page, "anchor")
                if not anchor_frame:
                    log("未检测到 reCAPTCHA，检查是否已直接成功")
                    new_expire = get_expire_time(page)
                    if new_expire != old_expire and new_expire != "未知":
                        success = True
                    else:
                        failure_reason = "未找到 reCAPTCHA 验证码区域"
                    break

                log("启动 reCAPTCHA 音频破解...")
                try:
                    solved = solve_recaptcha(page)
                except CaptchaBlocked:
                    log("IP 被封锁，换 IP 后重试", "WARN")
                    failure_reason = "IP 被 reCAPTCHA 封锁"
                    try:
                        page.quit()
                    except:
                        pass
                    page = None
                    if attempt < MAX_RENEW_RETRIES_PER_URL:
                        restart_warp()
                        continue
                    break
                except Exception as e:
                    log(f"reCAPTCHA 异常: {e}", "ERROR")
                    failure_reason = f"reCAPTCHA 异常: {e}"
                    break

                if not solved:
                    failure_reason = "未通过 reCAPTCHA 验证"
                    break

                log("点击最终 Renew 按钮")
                final_btn = page.ele('xpath://button[normalize-space(text())="Renew"]', timeout=3)
                if final_btn:
                    try:
                        final_btn.click()
                    except:
                        final_btn.click(by_js=True)
                    time.sleep(10)
                    new_expire = get_expire_time(page)
                    if new_expire != old_expire and new_expire != "未知":
                        log(f"到期时间已更新: {old_expire} -> {new_expire}")
                        success = True
                    else:
                        page_text = (page.html or "").lower()
                        if any(w in page_text for w in ["successfully", "renewed"]):
                            success = True
                        else:
                            failure_reason = "续期后未检测到成功标志"
                else:
                    failure_reason = "找不到最终 Renew 按钮"
                break

            except Exception as e:
                log(f"续期尝试异常: {e}", "ERROR")
                failure_reason = f"运行异常: {str(e)[:200]}"
                if attempt < MAX_RENEW_RETRIES_PER_URL:
                    if page:
                        try:
                            page.quit()
                        except:
                            pass
                        page = None
                    restart_warp()
                    continue
                break
            finally:
                if page:
                    screen_name = f"host2play-{server_name}-{'success' if success else 'fail'}.png"
                    screenshot_path = capture_page_screenshot(page, os.path.join(screenshot_dir, screen_name))
                    try:
                        page.quit()
                    except:
                        pass
    finally:
        vdisplay.stop()

    return success, server_name, old_expire, new_expire, screenshot_path, failure_reason

# ==============================================================================
# 主入口
# ==============================================================================
def main():
    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    if not RENEW_URLS:
        log("请在 RENEW_URLS 列表中添加续期链接", "ERROR")
        sys.exit(1)

    total_success = 0
    for idx, url in enumerate(RENEW_URLS, 1):
        log(f"{'#'*60}")
        log(f"处理第 {idx} 个链接: {url}")
        log(f"{'#'*60}")

        success, server_name, old_expire, new_expire, screenshot, failure_reason = renew_single_url(url)

        if success:
            caption = build_notification(True, url, server_name, old_expire, new_expire)
            total_success += 1
        else:
            caption = build_notification(False, url, server_name, old_expire, failure_reason=failure_reason)

        send_tg_photo(tg_token, tg_chat_id, screenshot, caption, parse_mode='HTML')

    log(f"全部完成，成功 {total_success}/{len(RENEW_URLS)} 个链接")
    if total_success < len(RENEW_URLS):
        sys.exit(1)

if __name__ == "__main__":
    main()
