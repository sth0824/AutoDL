# AutoDL

URL만 붙여넣으면 웹사이트의 영상을 **가능한 최고화질**로 자동 다운로드하는 데스크톱 앱.

[yt-dlp](https://github.com/yt-dlp/yt-dlp)(유튜브·인스타그램·트위터 등 수천 개 사이트 지원)와
ffmpeg를 감싸, 최고 해상도 영상과 최고 음질 오디오를 받아 MP4로 합쳐줍니다.

## 설치

```bash
pip install -r requirements.txt
```

> ffmpeg는 시스템에 설치돼 있으면 그걸 쓰고, 없으면 `imageio-ffmpeg`(pip로 함께 설치됨)에
> 포함된 빌드를 자동으로 사용합니다. 별도 설치가 필요 없어요.

## 실행

```bash
python autodl.py
```

1. 영상 URL을 붙여넣고
2. 저장 폴더를 고른 뒤
3. **⬇ 최고화질로 다운로드** 클릭

### YouTube 최상위 화질 (선택)

최신 yt-dlp는 일부 YouTube 영상의 최고화질 포맷을 뽑을 때 JS 런타임([Deno](https://deno.com))을
요구합니다. 없어도 대부분 4K까지 받아지지만, 간혹 최상위 포맷이 누락될 수 있어요.
최대치를 보장하려면 Deno를 설치하세요(Windows): `winget install DenoLand.Deno`

## 동작 방식

- 화질: `bv*+ba/b` — 받을 수 있는 최고 화질 영상 + 최고 음질 오디오를 따로 받아 합칩니다.
- 합치기: 재인코딩 없이 컨테이너만 MP4로 바꾸므로 **화질 손실이 없습니다**.
  (MP4에 담을 수 없는 코덱이면 원본 컨테이너를 유지)

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `autodl.py` | tkinter GUI |
| `downloader.py` | yt-dlp 래퍼(다운로드 엔진). GUI/스크립트에서 공유 |
| `requirements.txt` | 의존성 |

## 주의

본인이 권리를 갖고 있거나 다운로드가 허용된 콘텐츠에만 사용하세요. 각 사이트의
이용약관과 저작권법을 준수하는 것은 사용자 책임입니다.
