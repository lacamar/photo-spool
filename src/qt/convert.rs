use cxx_qt_lib::{QList, QMap, QMapPair_QString_QVariant, QString, QVariant};
use serde_json::Value;

pub fn to_qvariant(v: &Value) -> QVariant {
    match v {
        Value::Null => QVariant::default(),
        Value::Bool(b) => QVariant::from(b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => i32::try_from(i).map_or_else(|_| QVariant::from(&i), |i| QVariant::from(&i)),
            None => QVariant::from(&n.as_f64().unwrap_or_default()),
        },
        Value::String(s) => QVariant::from(&QString::from(s.as_str())),
        Value::Array(a) => {
            let mut list = QList::<QVariant>::default();
            for item in a {
                list.append(to_qvariant(item));
            }
            QVariant::from(&list)
        }
        Value::Object(o) => {
            let mut map = QMap::<QMapPair_QString_QVariant>::default();
            for (k, item) in o {
                map.insert(QString::from(k.as_str()), to_qvariant(item));
            }
            QVariant::from(&map)
        }
    }
}

/// Settings values arrive from QML as bool or string; anything else is kept as its string form.
pub fn from_qvariant(v: &QVariant) -> Value {
    match v.type_name() {
        "bool" => Value::Bool(v.value::<bool>().unwrap_or_default()),
        "int" | "qlonglong" => v.value::<i64>().map_or(Value::Null, Value::from),
        "double" => v.value::<f64>().map_or(Value::Null, Value::from),
        "" => Value::Null,
        _ => v
            .value::<QString>()
            .map_or(Value::Null, |s| Value::String(s.to_string())),
    }
}

pub fn strings(list: &QList<QVariant>) -> Vec<String> {
    list.iter()
        .filter_map(|v| v.value::<QString>())
        .map(|s| s.to_string())
        .collect()
}
