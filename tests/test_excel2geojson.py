import json
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dart_tool.excel2geojson import export_sheet


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _write_workbook(path, headers, rows):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        workbook = ET.Element(f"{{{MAIN_NS}}}workbook")
        sheets = ET.SubElement(workbook, f"{{{MAIN_NS}}}sheets")
        ET.SubElement(
            sheets,
            f"{{{MAIN_NS}}}sheet",
            {
                "name": "Operating",
                "sheetId": "1",
                f"{{{REL_NS}}}id": "rId1",
            },
        )
        archive.writestr(
            "xl/workbook.xml", ET.tostring(workbook, xml_declaration=True)
        )

        relationships = ET.Element(f"{{{PACKAGE_REL_NS}}}Relationships")
        ET.SubElement(
            relationships,
            f"{{{PACKAGE_REL_NS}}}Relationship",
            {
                "Id": "rId1",
                "Type": f"{REL_NS}/worksheet",
                "Target": "worksheets/sheet1.xml",
            },
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            ET.tostring(relationships, xml_declaration=True),
        )

        worksheet = ET.Element(f"{{{MAIN_NS}}}worksheet")
        sheet_data = ET.SubElement(worksheet, f"{{{MAIN_NS}}}sheetData")
        for row_number, values in [(3, headers), *enumerate(rows, start=4)]:
            row = ET.SubElement(
                sheet_data, f"{{{MAIN_NS}}}row", {"r": str(row_number)}
            )
            for column_index, value in enumerate(values, start=1):
                if value is None:
                    continue
                cell = ET.SubElement(
                    row,
                    f"{{{MAIN_NS}}}c",
                    {"r": f"{_column_name(column_index)}{row_number}"},
                )
                if isinstance(value, str):
                    cell.set("t", "inlineStr")
                    inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
                    ET.SubElement(inline, f"{{{MAIN_NS}}}t").text = value
                else:
                    cell.set("t", "n")
                    ET.SubElement(cell, f"{{{MAIN_NS}}}v").text = str(value)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            ET.tostring(worksheet, xml_declaration=True),
        )


class ExcelToGeoJSONTests(unittest.TestCase):
    def test_export_preserves_columns_and_skips_invalid_coordinates(self):
        headers = [
            "Plant Name",
            "Net Summer Capacity (MW)",
            "Latitude",
            "Longitude",
            "Technology",
        ]
        rows = [
            ["Sample Wind", 12.5, 40.1, -75.2, "Wind"],
            ["Missing Coordinates", 3, None, -70, "Gas"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "plants.xlsx"
            output_path = Path(directory) / "operating.geojson"
            _write_workbook(workbook_path, headers, rows)

            exported, skipped = export_sheet(
                workbook_path, "Operating", output_path
            )

            collection = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual((exported, skipped), (1, 1))
        self.assertFalse(output_path.with_name(output_path.name + ".tmp").exists())
        self.assertEqual(collection["type"], "FeatureCollection")
        feature = collection["features"][0]
        self.assertEqual(feature["id"], "operating-4")
        self.assertEqual(feature["geometry"]["coordinates"], [-75.2, 40.1])
        self.assertEqual(feature["properties"]["Plant Name"], "Sample Wind")
        self.assertEqual(feature["properties"]["Net Summer Capacity (MW)"], 12.5)
        self.assertEqual(feature["properties"]["sheet_status"], "operating")

    def test_export_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "plants.xlsx"
            output_path = Path(directory) / "operating.geojson"
            output_path.write_text("existing output", encoding="utf-8")
            _write_workbook(
                workbook_path,
                ["Plant Name", "Latitude"],
                [["Sample Wind", 40.1]],
            )

            with self.assertRaisesRegex(ValueError, "missing required columns"):
                export_sheet(workbook_path, "Operating", output_path)

            self.assertEqual(
                output_path.read_text(encoding="utf-8"), "existing output"
            )
            self.assertFalse(output_path.with_name(output_path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
