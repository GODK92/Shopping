"""쇼핑 리스트 앱 (Streamlit).

실행: streamlit run app.py

데이터 흐름
- 파일(shopping_list.json)이 유일한 원본이다. 매 실행마다 파일을 다시 읽어 화면을 그린다.
- 변경은 항상 "잠금 → 파일 다시 읽기 → 수정 → 원자적 저장" 순서로 한다(mutate).
  그래서 탭을 여러 개 열어도 서로의 변경을 덮어쓰지 않는다.
"""

import html
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import streamlit as st

DATA_FILE = Path(__file__).parent / "shopping_list.json"
MAX_NAME_LEN = 50

# load_items()가 돌려주는 상태 값
STATUS_OK = "ok"                # 정상 (파일이 없는 경우 포함)
STATUS_REPAIRED = "repaired"    # 읽었지만 잘못된 항목을 고쳤음 → 다시 저장 필요
STATUS_CORRUPTED = "corrupted"  # JSON이 깨졌거나 형식이 다름
STATUS_UNREADABLE = "unreadable"  # 권한 등으로 파일을 읽을 수 없음

_FAILED = object()  # mutate()가 저장에 실패했음을 나타내는 표식


# ---------------------------------------------------------------------------
# 데이터 함수 (Streamlit 위젯과 무관한 순수 로직)
# ---------------------------------------------------------------------------
def _parse_checked(value: object) -> bool:
    """checked 값을 bool로 해석한다. 문자열 "false"가 True가 되지 않도록 한다."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return value == 1
    return False


def _normalize(raw: object, seen_ids: set[str]) -> dict | None:
    """파일에서 읽은 항목 하나를 데이터 모델 형태로 정리한다. 쓸 수 없으면 None.

    id가 없거나 이미 나온 id면 새로 만든다 (위젯 key 중복으로 앱이 멈추는 것을 방지).
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", "")).strip()[:MAX_NAME_LEN]
    if not name:
        return None
    try:
        quantity = max(1, int(raw.get("quantity", 1)))
    except (TypeError, ValueError):
        quantity = 1
    item_id = str(raw.get("id") or "")
    if not item_id or item_id in seen_ids:
        item_id = str(uuid.uuid4())
    seen_ids.add(item_id)
    return {
        "id": item_id,
        "name": name,
        "quantity": quantity,
        "checked": _parse_checked(raw.get("checked", False)),
        "created_at": str(raw.get("created_at") or datetime.now().isoformat(timespec="seconds")),
    }


def load_items() -> tuple[list[dict], str]:
    """JSON 파일에서 아이템 목록을 읽는다.

    Returns:
        (items, status). status는 STATUS_* 중 하나.
        STATUS_REPAIRED면 정리된 목록을 다시 저장해야 id가 고정된다.
    """
    if not DATA_FILE.exists():
        return [], STATUS_OK
    try:
        raw_bytes = DATA_FILE.read_bytes()
    except OSError:
        return [], STATUS_UNREADABLE
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return [], STATUS_CORRUPTED
    if not isinstance(data, list):
        return [], STATUS_CORRUPTED

    seen_ids: set[str] = set()
    items = [item for item in (_normalize(raw, seen_ids) for raw in data) if item]
    status = STATUS_OK if items == data else STATUS_REPAIRED
    return items, status


def save_items(items: list[dict]) -> None:
    """아이템 목록을 JSON 파일로 원자적으로 저장한다.

    임시 파일에 다 쓴 뒤 교체하므로, 저장 도중 중단되어도 기존 파일이 깨지지 않는다.
    """
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, DATA_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def quarantine_corrupted_file() -> Path | None:
    """손상된 데이터 파일을 지우지 않고 옆으로 옮겨 둔다. 옮긴 경로를 반환한다."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA_FILE.with_name(f"{DATA_FILE.stem}.corrupted-{stamp}{DATA_FILE.suffix}")
    try:
        os.replace(DATA_FILE, backup)
    except OSError:
        return None
    return backup


def validate_name(name: str) -> str | None:
    """이름이 올바르면 None, 아니면 경고 메시지를 반환한다."""
    if not name.strip():
        return "아이템 이름을 입력해 주세요."
    if len(name.strip()) > MAX_NAME_LEN:
        return f"아이템 이름은 {MAX_NAME_LEN}자 이하로 입력해 주세요."
    return None


def add_item(items: list[dict], name: str, quantity: int = 1) -> dict:
    """새 아이템을 만들어 목록에 추가하고 반환한다."""
    item = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "quantity": max(1, int(quantity)),
        "checked": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    items.append(item)
    return item


def find_item(items: list[dict], item_id: str | None) -> dict | None:
    """id에 해당하는 아이템을 찾는다."""
    return next((item for item in items if item["id"] == item_id), None)


def update_item(items: list[dict], item_id: str, name: str, quantity: int) -> bool:
    """id에 해당하는 아이템의 이름과 수량을 수정한다. 아이템이 없으면 False."""
    item = find_item(items, item_id)
    if not item:
        return False
    item["name"] = name.strip()
    item["quantity"] = max(1, int(quantity))
    return True


def delete_item(items: list[dict], item_id: str) -> dict | None:
    """id에 해당하는 아이템을 삭제하고 반환한다. 없으면 None."""
    item = find_item(items, item_id)
    if item:
        items.remove(item)
    return item


def set_checked(items: list[dict], item_id: str, checked: bool) -> None:
    """id에 해당하는 아이템의 체크 상태를 지정한 값으로 바꾼다.

    뒤집기(toggle)가 아니라 값 지정이라, 다른 탭에서 먼저 바뀌었어도 화면에 보이는 대로 저장된다.
    """
    item = find_item(items, item_id)
    if item:
        item["checked"] = bool(checked)


def clear_checked(items: list[dict]) -> int:
    """체크된 아이템을 모두 삭제하고, 삭제한 개수를 반환한다."""
    before = len(items)
    items[:] = [item for item in items if not item["checked"]]
    return before - len(items)


def sorted_items(items: list[dict]) -> list[dict]:
    """미완료가 위, 완료가 아래. 각 그룹 안에서는 최근에 추가한 것이 위 (PRD AC-1).

    목록은 추가한 순서대로 저장되므로, 목록 위치를 기준으로 역순 정렬한다.
    """
    indexed = sorted(enumerate(items), key=lambda pair: (pair[1]["checked"], -pair[0]))
    return [item for _, item in indexed]


def item_label_html(item: dict) -> str:
    """아이템 이름과 수량을 HTML로 만든다.

    사용자 입력은 반드시 html.escape를 거친다 (XSS 방지).
    <div>로 시작하는 HTML 블록이라 내부 텍스트는 마크다운으로 해석되지 않는다.
    """
    done = " done" if item["checked"] else ""
    return (
        f'<div class="item-name{done}">{html.escape(item["name"])}'
        f'<span class="item-qty">x{int(item["quantity"])}</span></div>'
    )


# ---------------------------------------------------------------------------
# 저장소 접근 (잠금 + 파일 읽기/쓰기)
# ---------------------------------------------------------------------------
@st.cache_resource
def _file_lock() -> threading.RLock:
    """모든 세션(탭)이 공유하는 파일 잠금. 스크립트가 다시 실행되어도 같은 객체를 쓴다."""
    return threading.RLock()


def load_store() -> list[dict]:
    """파일에서 목록을 읽는다."""
    return _load_store_with_status()[0]


def _load_store_with_status() -> tuple[list[dict], str]:
    """파일에서 목록과 상태를 읽는다. 손상/정리 상황은 여기서 처리하고 사용자에게 알린다."""
    with _file_lock():
        items, status = load_items()
        if status == STATUS_CORRUPTED:
            backup = quarantine_corrupted_file()
            if backup:
                notify("warning", f"저장 파일이 손상되어 `{backup.name}`(으)로 옮겨 두고 빈 리스트로 시작합니다.")
            else:
                notify("error", "저장 파일이 손상되었고 백업도 하지 못했습니다. 파일을 직접 확인해 주세요.")
        elif status == STATUS_UNREADABLE:
            notify("error", "저장 파일을 읽을 수 없습니다. 파일 권한을 확인해 주세요.")
        elif status == STATUS_REPAIRED:
            try:
                save_items(items)
            except OSError:
                pass  # 정리본 저장에 실패해도 화면은 정리된 목록으로 보여준다
        return items, status


def mutate(change: Callable[[list[dict]], Any]) -> Any:
    """잠금을 잡고 최신 파일을 읽어 change를 적용한 뒤 저장한다.

    Returns:
        change의 반환값. 저장에 실패하면 _FAILED (변경은 반영되지 않음).
    """
    with _file_lock():
        items, status = _load_store_with_status()
        if status == STATUS_UNREADABLE:
            # 읽지 못한 파일을 빈 목록 기준으로 덮어쓰면 데이터가 사라지므로 중단한다.
            return _FAILED
        result = change(items)
        try:
            save_items(items)
        except OSError as e:
            notify("error", f"저장에 실패해 변경 내용이 반영되지 않았습니다: {e}")
            return _FAILED
        return result


# ---------------------------------------------------------------------------
# 상태와 콜백
# ---------------------------------------------------------------------------
def init_state() -> None:
    """세션별 UI 상태만 초기화한다. 아이템 목록은 session_state에 두지 않는다."""
    st.session_state.setdefault("editing_id", None)
    st.session_state.setdefault("messages", [])


def notify(level: str, text: str) -> None:
    """화면에 보여줄 메시지를 쌓는다 (콜백 안에서는 바로 그릴 수 없음)."""
    st.session_state.setdefault("messages", []).append((level, text))


def on_add() -> None:
    name = st.session_state.get("new_name", "")
    error = validate_name(name)
    if error:
        notify("warning", error)
        return
    qty = st.session_state.get("new_qty", 1)
    item = mutate(lambda items: add_item(items, name, qty))
    if item is not _FAILED:
        notify("toast", f"'{item['name']}'을(를) 추가했어요.")


def on_toggle(item_id: str) -> None:
    checked = st.session_state.get(f"check_{item_id}", False)
    mutate(lambda items: set_checked(items, item_id, checked))


def on_start_edit(item_id: str) -> None:
    item = find_item(load_store(), item_id)
    if not item:
        notify("warning", "이미 삭제된 아이템이에요.")
        return
    st.session_state.editing_id = item_id
    # 편집 입력칸을 현재 값으로 채운다.
    st.session_state[f"edit_name_{item_id}"] = item["name"]
    st.session_state[f"edit_qty_{item_id}"] = item["quantity"]


def on_save_edit(item_id: str) -> None:
    name = st.session_state.get(f"edit_name_{item_id}", "")
    error = validate_name(name)
    if error:
        notify("warning", error)
        return  # 편집 모드 유지
    qty = st.session_state.get(f"edit_qty_{item_id}", 1)
    found = mutate(lambda items: update_item(items, item_id, name, qty))
    if found is _FAILED:
        return  # 편집 모드 유지 (오류 메시지는 mutate가 표시)
    st.session_state.editing_id = None
    if found:
        notify("toast", "수정했어요.")
    else:
        notify("warning", "이미 삭제된 아이템이라 수정하지 못했어요.")


def on_cancel_edit() -> None:
    st.session_state.editing_id = None


def on_delete(item_id: str) -> None:
    item = mutate(lambda items: delete_item(items, item_id))
    if item is _FAILED:
        return
    if st.session_state.editing_id == item_id:
        st.session_state.editing_id = None
    if item:
        notify("toast", f"'{item['name']}'을(를) 삭제했어요.")


def on_clear_checked() -> None:
    removed = mutate(clear_checked)
    if removed is not _FAILED and removed:
        notify("toast", f"체크된 항목 {removed}개를 삭제했어요.")


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
CSS = """
<style>
/* 레이아웃: 헤더 */
.app-subtitle { opacity: .65; margin: -0.75rem 0 1.25rem; }

/* 아이템 행: 행 사이 얇은 구분선 */
[class*="st-key-row_"] {
    border-bottom: 1px solid rgba(128, 128, 128, .18);
    padding: .2rem 0;
}
/* 이름이 길어도 버튼이 다음 줄로 밀리지 않게: 줄바꿈 금지 + 이름 칸만 늘고 줄어듦 */
[class*="st-key-row_"] { flex-wrap: nowrap !important; }
[class*="st-key-row_"] > div:nth-child(2) { flex: 1 1 0 !important; min-width: 0 !important; }
/* Streamlit 마크다운 기본 음수 여백 제거 (체크박스와 세로 정렬 맞춤) */
[class*="st-key-row_"] > div:nth-child(2) [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }

/* 아이콘 버튼: 글자 라벨은 화면에서만 숨기고 화면 낭독기에는 읽히게 한다 */
[class*="st-key-edit_"] button [data-testid="stMarkdownContainer"],
[class*="st-key-del_"] button [data-testid="stMarkdownContainer"],
[class*="st-key-save_"] button [data-testid="stMarkdownContainer"],
[class*="st-key-cancel_"] button [data-testid="stMarkdownContainer"] {
    position: absolute !important; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}

/* 아이템 이름 / 수량 */
.item-name { font-size: 1.02rem; line-height: 1.4; overflow-wrap: anywhere; }
.item-name.done { text-decoration: line-through; opacity: .45; }
.item-qty { font-size: .8rem; opacity: .55; margin-left: .45rem; white-space: nowrap; }
.item-name.done .item-qty { text-decoration: none; display: inline-block; }

/* 편집 중인 행 강조 */
.st-key-editing {
    background: rgba(46, 158, 91, .09);
    border: 1px solid rgba(46, 158, 91, .55) !important;
    border-radius: .6rem;
}
.st-key-editing_row { flex-wrap: nowrap !important; }
.st-key-editing_row > div:first-child { flex: 1 1 0 !important; min-width: 0 !important; }

/* 빈 상태 */
.empty-state { text-align: center; padding: 2.5rem 0 2rem; opacity: .7; }
.empty-state .icon { font-size: 2.6rem; line-height: 1; margin-bottom: .6rem; }
</style>
"""


def render_header() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    st.title("🛒 쇼핑 리스트")
    st.markdown(
        '<p class="app-subtitle">오늘 살 것을 적고, 산 것은 체크하세요</p>',
        unsafe_allow_html=True,
    )


def render_summary(items: list[dict]) -> None:
    total = len(items)
    done = sum(item["checked"] for item in items)
    # 가로 컨테이너는 모바일에서도 세로로 쌓이지 않는다.
    with st.container(horizontal=True, gap="small"):
        st.metric("전체", total, border=True)
        st.metric("남은", total - done, border=True)
        st.metric("완료", done, border=True)
    if total:
        st.progress(done / total, text=f"{round(done / total * 100)}% 완료")


def render_add_form() -> None:
    with st.form("add_form", clear_on_submit=True, border=False):
        col_name, col_qty, col_btn = st.columns([6, 2, 2], vertical_alignment="bottom")
        col_name.text_input(
            "아이템 이름", key="new_name", placeholder="예: 우유", max_chars=MAX_NAME_LEN
        )
        col_qty.number_input("수량", min_value=1, value=1, step=1, key="new_qty")
        col_btn.form_submit_button("추가", type="primary", width="stretch", on_click=on_add)


def render_edit_row(item: dict) -> None:
    """편집 중인 행. 폼으로 감싸 Enter로도 저장되게 한다 (첫 번째 제출 버튼 = 저장)."""
    item_id = item["id"]
    with st.container(border=True, key="editing"):
        with st.form(f"edit_form_{item_id}", border=False):
            with st.container(horizontal=True, vertical_alignment="center", gap="small", key="editing_row"):
                st.text_input(
                    "이름 수정", key=f"edit_name_{item_id}",
                    label_visibility="collapsed", max_chars=MAX_NAME_LEN,
                )
                st.number_input(
                    "수량 수정", min_value=1, step=1, key=f"edit_qty_{item_id}",
                    label_visibility="collapsed", width=80,
                )
                st.form_submit_button("저장", icon="💾", key=f"save_{item_id}", type="primary",
                                      on_click=on_save_edit, args=(item_id,))
                st.form_submit_button("취소", icon="✖", key=f"cancel_{item_id}",
                                      on_click=on_cancel_edit)


def render_item(item: dict, editing_other: bool) -> None:
    item_id = item["id"]

    if st.session_state.editing_id == item_id:
        render_edit_row(item)
        return

    # 파일의 최신 체크 상태를 위젯에 맞춘다 (다른 탭에서 바뀐 경우 포함).
    st.session_state[f"check_{item_id}"] = item["checked"]
    with st.container(horizontal=True, vertical_alignment="center", gap="small", key=f"row_{item_id}"):
        st.checkbox(
            f"{item['name']} 구매 완료",
            key=f"check_{item_id}",
            label_visibility="collapsed",
            on_change=on_toggle,
            args=(item_id,),
        )
        st.markdown(item_label_html(item), unsafe_allow_html=True, width="stretch")
        # 다른 아이템을 편집 중이면 수정 버튼을 막아, 저장하지 않은 수정 내용이 사라지지 않게 한다.
        st.button("수정", icon="✏️", key=f"edit_{item_id}", type="tertiary",
                  help="다른 아이템 편집을 먼저 끝내 주세요" if editing_other else "수정",
                  disabled=editing_other, on_click=on_start_edit, args=(item_id,))
        st.button("삭제", icon="🗑️", key=f"del_{item_id}", help="삭제", type="tertiary",
                  on_click=on_delete, args=(item_id,))


def render_empty_state() -> None:
    st.markdown(
        '<div class="empty-state"><div class="icon">🧺</div>'
        "쇼핑 리스트가 비어 있습니다. 아이템을 추가해 보세요!</div>",
        unsafe_allow_html=True,
    )


def render_messages() -> None:
    """쌓인 메시지를 한 번 보여주고 비운다."""
    for level, text in st.session_state.messages:
        if level == "toast":
            st.toast(text, icon="✅")
        else:
            getattr(st, level)(text)
    st.session_state.messages = []


def main() -> None:
    st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")
    init_state()
    items = load_store()

    # 편집 중이던 아이템이 (다른 탭 등에서) 삭제되었으면 편집 상태를 푼다.
    if st.session_state.editing_id and not find_item(items, st.session_state.editing_id):
        st.session_state.editing_id = None

    render_header()
    render_summary(items)
    render_add_form()
    render_messages()

    st.divider()

    if not items:
        render_empty_state()
        return

    editing_id = st.session_state.editing_id
    for item in sorted_items(items):
        render_item(item, editing_other=editing_id is not None and editing_id != item["id"])

    has_checked = any(item["checked"] for item in items)
    with st.container(horizontal=True, horizontal_alignment="right"):
        st.button("체크된 항목 모두 삭제", icon="🧹", disabled=not has_checked,
                  on_click=on_clear_checked)


main()
