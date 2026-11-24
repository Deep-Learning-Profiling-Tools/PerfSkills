/* Expose an existing printf timer value; do not introduce a new timer.
 * Only a configured literal metric prefix and one floating conversion qualify.
 * Computation, arguments, stream, and printf's original return count are preserved.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int rewrite(const char *format, char *output, size_t capacity) {
    const char *prefix = getenv("PW_METRIC_OUTPUT_PREFIX");
    const char *precision = getenv("PW_METRIC_OUTPUT_DIGITS");
    if (!prefix || !*prefix || !strstr(format, prefix) || !precision) return 0;
    int digits = atoi(precision);
    if (digits < 6 || digits > 17 || strlen(format) + 24 >= capacity) return 0;
    size_t begin = 0, end = 0;
    int floats = 0;
    for (size_t i = 0; format[i]; ++i) {
        if (format[i] != '%') continue;
        if (format[i + 1] == '%') { ++i; continue; }
        size_t start = i++;
        while (format[i] && !strchr("diouxXfFeEgGaAcspnm%", format[i])) ++i;
        if (!format[i]) return 0;
        if (format[i] == 'n') return 0;
        if (!strchr("fFeEgGaA", format[i])) continue;
        if (++floats > 1 || strchr("aA", format[i])) return 0;
        size_t length = i;
        while (length > start + 1 && strchr("hlLjzt", format[length - 1])) --length;
        size_t dot = start;
        while (dot < length && format[dot] != '.') ++dot;
        begin = end = length;
        if (dot < length) {
            if (format[dot + 1] == '*') return 0; /* preserve argument consumption */
            size_t p = dot + 1;
            while (isdigit((unsigned char)format[p])) ++p;
            if (atoi(format + dot + 1) >= digits) return 0;
            begin = dot; end = p;
        }
    }
    if (floats != 1) return 0;
    memcpy(output, format, begin);
    int count = snprintf(output + begin, capacity - begin, ".%d", digits);
    if (count < 0) return 0;
    strcpy(output + begin + count, format + end);
    return 1;
}

#ifdef __GLIBC__
extern int __vfprintf_chk(FILE *, int, const char *, va_list);
#endif
static int emit(FILE *stream, int checked, int flags, const char *format, va_list arguments) {
    char output[8192];
    const char *selected = format;
    int modified = rewrite(format, output, sizeof(output));
    int original_count = -1;
    if (modified) {
        va_list copy; va_copy(copy, arguments);
        original_count = vsnprintf(NULL, 0, format, copy);
        va_end(copy); selected = output;
    }
    int result;
#ifdef __GLIBC__
    result = checked ? __vfprintf_chk(stream, flags, selected, arguments) : vfprintf(stream, selected, arguments);
#else
    (void)checked; (void)flags; result = vfprintf(stream, selected, arguments);
#endif
    return result < 0 || original_count < 0 ? result : original_count;
}

int printf(const char *format, ...) {
    va_list args; va_start(args, format);
    int result = emit(stdout, 0, 0, format, args); va_end(args); return result;
}

#ifdef __GLIBC__
int __printf_chk(int flags, const char *format, ...) {
    va_list args; va_start(args, format);
    int result = emit(stdout, 1, flags, format, args); va_end(args); return result;
}
#endif
