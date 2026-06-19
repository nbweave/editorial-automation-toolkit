@echo off
setlocal EnableDelayedExpansion

:: Файл со списком ссылок лежит рядом со скриптом
set "URL_FILE=%~dp0urls.txt"

if not exist "%URL_FILE%" (
    echo Не найден файл: "%URL_FILE%"
    exit /b 1
)

set "opened=0"

:: Читаем файл построчно, пропуская пустые и закомментированные строки
for /f "usebackq tokens=* delims=" %%L in ("%URL_FILE%") do (
    set "LINE=%%L"
    :: Срезаем ведущие пробелы
    for /f "tokens=* delims= " %%T in ("!LINE!") do set "LINE=%%T"
    if defined LINE (
        set "FIRST=!LINE:~0,1!"
        if not "!FIRST!"=="#" if not "!FIRST!"==";" (
            set "URL=!LINE!"
            echo Открываю: !URL!
            start "" "browser" "!URL!"
            set /a opened+=1 >nul
        )
    )
)

if "%opened%"=="0" (
    echo В файле "urls.txt" не найдено ссылок.
) else (
    echo Готово. Открыто ссылок: %opened%
)

exit /b 0
