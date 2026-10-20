#!/usr/bin/env bash
# 供 Start.command 和 TikTokLiveTranslator.app 的启动脚本一起 source 的探测函数。
# 这份文件本身不执行任何东西——只定义函数，调用方 source 之后自己调用
# find_python <项目根目录>。
#
# 先找建 .venv 的那个解释器（find_venv_python），找不到再按常见安装位置找
# Python 3.9+（find_installed_python）。
#
# 为什么先认 .venv：系统的「完全磁盘访问权限」按**启动用的解释器文件路径**登记
# （见 app/browser_login.py 的 fda_targets）。2026-10-19 Homebrew 升级 yt-dlp 时顺带
# 装上 python@3.15，/opt/homebrew/bin/python3 从 3.14 变成 3.15；以前这里不看 .venv、
# 按安装位置挑第一个，于是程序改由 python3.15 启动——它没有这项权限，Safari 的登录态
# 读不到了。.venv 由 /opt/anaconda3/bin/python3.13 建成，本身一点没变。
# 启动用的解释器跟着 .venv 走，别的 Python 装上、升级都不会把启动换掉。建环境的
# 那个被卸掉、或者同一路径上换了次版本时，才落回原来的顺序，之后照旧交给 main.py 的
# ensure_env（.venv 跑不起来就用这次启动的解释器重建）。没有 .venv 的新装机行为不变。
#
# 只用 macOS 自带 /bin/bash 3.2 有的写法：没有关联数组、${var,,}、mapfile。

# 去掉两头的空白（含 \r）
_tlt_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# pyvenv.cfg 里 key 第一次出现时的值；没有这个 key 返回 1。$1=文件 $2=key
_tlt_cfg_value() {
  local line key
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in *=*) ;; *) continue ;; esac
    key="$(_tlt_trim "${line%%=*}")"
    if [ "$key" = "$2" ]; then
      _tlt_trim "${line#*=}"
      return 0
    fi
  done < "$1"
  return 1
}

# "3.13.5" / "3.13.5.final.0"（virtualenv 写 version_info）/ "3.13" → "3.13"
_tlt_major_minor() {
  local v="$1" major rest minor
  major="${v%%.*}"
  rest="${v#*.}"
  [ "$rest" != "$v" ] || return 1
  minor="${rest%%.*}"
  case "$major" in ''|*[!0-9]*) return 1 ;; esac
  case "$minor" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s.%s' "$major" "$minor"
}

# 目录的物理路径（解开链接）；目录不存在返回 1。CDPATH 置空：否则 cd 会把路径打到 stdout
_tlt_physical_dir() {
  (CDPATH='' cd -P -- "$1" 2>/dev/null && pwd -P)
}

# 文件所在目录（"/python3" 的目录是 "/"）
_tlt_dir_of() {
  local d="${1%/*}"
  printf '%s' "${d:-/}"
}

# 苹果自带的 /usr/bin/python3 只是开发者工具的垫片，未装过 Xcode 命令行工具时一执行
# 就会弹「安装开发者工具」对话框。必须确认它真正派发到的文件存在才能碰——
# xcode-select -p 只回显配置路径、不校验目录还在不在
_tlt_clt_python_ok() {
  local devdir
  devdir="$(xcode-select -p 2>/dev/null)"
  [ -n "$devdir" ] && [ -x "$devdir/usr/bin/python3" ]
}

# $1 能不能当「建 .venv 的解释器」用。$2=pyvenv.cfg 记的主次版本 $3=项目根
_tlt_venv_candidate_ok() {
  local out dir root nl='
'
  case "$1" in /*) ;; *) return 1 ;; esac
  [ -f "$1" ] && [ -x "$1" ] || return 1
  # 项目目录里的解释器只可能是 .venv 自己或 .app 里那个链回 .venv 的 python——
  # pyvenv.cfg 指回自己时不能拿它当建环境的那个
  dir="$(_tlt_physical_dir "$(_tlt_dir_of "$1")")" || return 1
  root="$(_tlt_physical_dir "$3")" || return 1
  case "$dir/" in "$root"/*) return 1 ;; esac
  if [ "$dir" = /usr/bin ]; then
    _tlt_clt_python_ok || return 1
  fi
  out="$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2]); raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null)" || return 1
  out="${out##*"$nl"}"     # 只看最后一行：站点钩子往 stdout 多打一行也不误判
  [ "$out" = "$2" ]
}

# 建 <项目根>/.venv 的那个解释器：pyvenv.cfg 的 executable，没有或用不了再试 home 下的
# python<主.次> 和 python3。必须还在、能跑、≥3.9、主次版本和 cfg 记的一样（同一路径上
# 的 python3 可能已经被升级成别的版本，这正是 10-19 的事）。cfg 不记版本就不认。
find_venv_python() {
  local root="${1:-$PWD}" cfg v want home p
  cfg="$root/.venv/pyvenv.cfg"
  [ -f "$cfg" ] || return 1
  v="$(_tlt_cfg_value "$cfg" version)" || v="$(_tlt_cfg_value "$cfg" version_info)" || return 1
  want="$(_tlt_major_minor "$v")" || return 1
  if p="$(_tlt_cfg_value "$cfg" executable)" && _tlt_venv_candidate_ok "$p" "$want" "$root"; then
    echo "$p"
    return 0
  fi
  home="$(_tlt_cfg_value "$cfg" home)" || return 1
  home="${home%/}"
  [ -n "$home" ] || return 1
  for p in "$home/python$want" "$home/python3"; do
    if _tlt_venv_candidate_ok "$p" "$want" "$root"; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

# 按常见安装位置寻找 Python 3.9+。顺序很重要：python.org / Homebrew 的真实
# 安装优先；/usr/bin/python3 垫片必须先确认装过开发者工具才能碰。
find_installed_python() {
  local candidates=() p v
  # python.org 官方框架：全部版本按新到旧依次候选（老版本过不了 ≥3.9 检查时
  # 还能落到下一个，不能只挑一个）
  for v in $(ls /Library/Frameworks/Python.framework/Versions 2>/dev/null \
             | grep -E '^3\.[0-9]+$' | sort -t. -k2,2nr); do
    candidates+=("/Library/Frameworks/Python.framework/Versions/$v/bin/python3")
  done
  candidates+=(/opt/homebrew/bin/python3 /usr/local/bin/python3)
  if _tlt_clt_python_ok; then
    candidates+=(/usr/bin/python3)
  fi
  for p in "${candidates[@]}"; do
    if [ -x "$p" ] && "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  # 终端登录 shell 里 PATH 是全的，pyenv/conda 等安装也一并尝试
  p="$(command -v python3 2>/dev/null)"
  if [ -n "$p" ] && [ "$p" != "/usr/bin/python3" ] && \
     "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    echo "$p"
    return 0
  fi
  return 1
}

# $1=项目根目录（含 .venv 的那一层），不给就用当前目录
find_python() {
  find_venv_python "${1:-$PWD}" || find_installed_python
}
