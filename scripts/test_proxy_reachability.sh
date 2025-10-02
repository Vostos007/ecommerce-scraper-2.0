#!/bin/bash

# Скрипт для тестирования достижимости прокси
# Использование: ./scripts/test_proxy_reachability.sh [target_url]

set -euo pipefail

trap 'rm -f /tmp/proxy_test_$$.*' EXIT

TARGET_URL="https://ili-ili.com"
DETAILED=false
MEASURE_LATENCY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --detailed)
            DETAILED=true
            shift
            ;;
        --latency)
            MEASURE_LATENCY=true
            shift
            ;;
        -*)
            echo "Неизвестный флаг: $1" >&2
            exit 1
            ;;
        *)
            TARGET_URL="$1"
            shift
            ;;
    esac
done
PROXY_FILE="config/manual_proxies.txt"
TIMEOUT=10
RESULTS_FILE="logs/proxy_test_$(date +%Y%m%d_%H%M%S).log"

echo "🔍 Тестирование прокси для $TARGET_URL"
echo "📝 Результаты: $RESULTS_FILE"
echo "=================================="

mkdir -p logs

# Счетчики
TOTAL=0
SUCCESS=0
FAILED=0

test_proxy() {
    local proxy_url="$1"
    local index="$2"
    
    echo -n "[$index] Тест $proxy_url ... "
    
    # Определяем протокол
    if [[ $proxy_url == socks5://* ]]; then
        PROXY_PROTOCOL="socks5"
    elif [[ $proxy_url == https://* ]]; then
        PROXY_PROTOCOL="https"
    elif [[ $proxy_url == http://* ]]; then
        PROXY_PROTOCOL="http"
    else
        echo "❌ Неизвестный протокол"
        return 1
    fi
    
    # Тест 1: Базовая проверка подключения
    if timeout $TIMEOUT curl -x "$proxy_url" \
        --connect-timeout $TIMEOUT \
        -s -o /dev/null -w "%{http_code}" \
        "$TARGET_URL" > /tmp/proxy_test_$$.status 2>/tmp/proxy_test_$$.error; then
        
        STATUS=$(cat /tmp/proxy_test_$$.status)
        
        if [ "$STATUS" = "000" ]; then
            echo "❌ ConnectTimeout (не удалось установить соединение)"
            echo "[$index] $proxy_url - FAILED (ConnectTimeout)" >> "$RESULTS_FILE"
            return 1
        elif [ "$STATUS" -ge 200 ] && [ "$STATUS" -lt 600 ]; then
            echo "✅ Работает (HTTP $STATUS)"
            echo "[$index] $proxy_url - SUCCESS (HTTP $STATUS)" >> "$RESULTS_FILE"
            
            # Дополнительная информация
            RESPONSE_TIME=$(timeout $TIMEOUT curl -x "$proxy_url" \
                --connect-timeout $TIMEOUT \
                -s -o /dev/null -w "%{time_total}" \
                "$TARGET_URL" 2>/dev/null || echo "N/A")
            echo "    ⏱️  Время ответа: ${RESPONSE_TIME}s" | tee -a "$RESULTS_FILE"

            if $MEASURE_LATENCY; then
                read CONNECT_TIME START_TRANSFER TOTAL_TIME < <(
                    timeout $TIMEOUT curl -x "$proxy_url" \
                        --connect-timeout $TIMEOUT \
                        -s -o /dev/null \
                        -w "%{time_connect} %{time_starttransfer} %{time_total}" \
                        "$TARGET_URL" 2>/dev/null || echo "N/A N/A N/A"
                )
                echo "    ⚡ Latency: connect=${CONNECT_TIME}s start_transfer=${START_TRANSFER}s total=${TOTAL_TIME}s" | tee -a "$RESULTS_FILE"
            fi

            if $DETAILED; then
                timeout $TIMEOUT curl -x "$proxy_url" \
                    --connect-timeout $TIMEOUT \
                    -s -D /tmp/proxy_test_$$.headers \
                    -o /tmp/proxy_test_$$.preview \
                    "$TARGET_URL" >/dev/null 2>&1 || true

                if [ -s /tmp/proxy_test_$$.headers ]; then
                    head -n 5 /tmp/proxy_test_$$.headers | sed 's/^/    📄 /' | tee -a "$RESULTS_FILE"
                fi
                if [ -s /tmp/proxy_test_$$.preview ]; then
                    head -c 120 /tmp/proxy_test_$$.preview | sed 's/^/    🔎 /' | tee -a "$RESULTS_FILE"
                fi
            fi

            return 0
        else
            echo "⚠️  Странный статус: $STATUS"
            echo "[$index] $proxy_url - WARNING (HTTP $STATUS)" >> "$RESULTS_FILE"
            return 0
        fi
    else
        ERROR=$(cat /tmp/proxy_test_$$.error 2>/dev/null | head -n 1 || echo "Unknown error")
        echo "❌ Ошибка: $ERROR"
        echo "[$index] $proxy_url - FAILED ($ERROR)" >> "$RESULTS_FILE"
        if $DETAILED && [ -s /tmp/proxy_test_$$.error ]; then
            head -n 3 /tmp/proxy_test_$$.error | sed 's/^/    ⚠️  /' | tee -a "$RESULTS_FILE"
        fi
        return 1
    fi
    
    rm -f /tmp/proxy_test_$$.status /tmp/proxy_test_$$.error /tmp/proxy_test_$$.headers /tmp/proxy_test_$$.preview
}

# Основной цикл тестирования
if [ ! -f "$PROXY_FILE" ]; then
    echo "❌ Файл $PROXY_FILE не найден"
    exit 1
fi

echo "" > "$RESULTS_FILE"
echo "🧪 Тест прокси для $TARGET_URL - $(date)" >> "$RESULTS_FILE"
echo "================================" >> "$RESULTS_FILE"

INDEX=1
while IFS= read -r proxy_line || [ -n "$proxy_line" ]; do
    # Пропускаем пустые строки и комментарии
    [[ -z "$proxy_line" || "$proxy_line" =~ ^# ]] && continue
    
    TOTAL=$((TOTAL + 1))
    
    if test_proxy "$proxy_line" "$INDEX"; then
        SUCCESS=$((SUCCESS + 1))
    else
        FAILED=$((FAILED + 1))
    fi
    
    INDEX=$((INDEX + 1))
    sleep 1  # Небольшая задержка между тестами
    
done < "$PROXY_FILE"

# Итоговая статистика
echo ""
echo "================================"
echo "📊 Итоги тестирования:"
echo "   Всего: $TOTAL"
echo "   ✅ Успешно: $SUCCESS"
echo "   ❌ Не работают: $FAILED"
echo "   📈 Успешность: $(awk "BEGIN {printf \"%.1f\", ($SUCCESS/$TOTAL)*100}")%"
echo ""
echo "📝 Полный отчет: $RESULTS_FILE"

# Рекомендации
if [ $SUCCESS -eq 0 ]; then
    echo ""
    echo "⚠️  РЕКОМЕНДАЦИИ:"
    echo "   1. Проверьте, что прокси-сервера доступны из вашей сети"
    echo "   2. Убедитесь, что ili-ili.com не блокирует IP-адреса прокси"
    echo "   3. Попробуйте прокси ближе к Московскому региону"
    echo "   4. Проверьте учетные данные прокси"
elif [ $SUCCESS -lt $((TOTAL / 2)) ]; then
    echo ""
    echo "⚠️  Менее 50% прокси работают. Рекомендуется обновить список."
fi

exit 0
