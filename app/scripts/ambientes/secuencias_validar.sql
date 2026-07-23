DO $$
DECLARE
    r RECORD;
    v_last_value BIGINT;
    v_max_id BIGINT;
BEGIN
    FOR r IN
        SELECT 
            n.nspname AS schema_name,
            c.relname AS table_name,
            a.attname AS column_name,
            pg_get_serial_sequence(
                format('%I.%I', n.nspname, c.relname),
                a.attname
            ) AS sequence_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE c.relkind = 'r'
          AND a.attnum > 0
          AND NOT a.attisdropped
    LOOP
        IF r.sequence_name IS NOT NULL THEN

            -- obtener valores
            EXECUTE format('SELECT last_value FROM %s', r.sequence_name)
            INTO v_last_value;

            EXECUTE format(
                'SELECT COALESCE(MAX(%I),0) FROM %I.%I',
                r.column_name,
                r.schema_name,
                r.table_name
            )
            INTO v_max_id;

            -- mostrar resultado
            RAISE NOTICE 
            '%.% -> seq=% | last=% | max=% | estado=%',
            r.schema_name,
            r.table_name,
            r.sequence_name,
            v_last_value,
            v_max_id,
            CASE 
                WHEN v_last_value >= v_max_id THEN 'OK'
                ELSE 'DESFASADA'
            END;

        END IF;
    END LOOP;
END$$;