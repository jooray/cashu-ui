from cProfile import label
import sys
from urllib.request import HTTPDefaultErrorHandler
from PyQt6 import QtWidgets, uic, QtGui, QtCore
from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import QWidget, QTableWidget

import asyncio
import os
import json
import time
from datetime import datetime
import base64
from itertools import groupby
from operator import itemgetter
from typing import List
from cashu.wallet import migrations
from cashu.wallet.crud import get_reserved_proofs, get_lightning_invoices
from cashu.core.helpers import fee_reserve, sum_proofs

from cashu.core.migrations import migrate_databases
from cashu.wallet.wallet import Wallet as Wallet
from cashu.core.settings import settings
from cashu.core.base import Proof, Invoice

from cashu.wallet.helpers import (
    deserialize_token_from_string,
    init_wallet,
    list_mints,
    receive,
    send,
)

import worker
import os
import traceback

walletname = "wallet"
db_path = os.path.join(settings.cashu_dir, walletname)
# allow to perform migrations
wallet = asyncio.run(Wallet.with_db(settings.mint_url, db_path, name = walletname, skip_private_key = True))
# load with private keys
wallet = asyncio.run(Wallet.with_db(settings.mint_url, db_path, name = walletname))


def table_headers(table: QTableWidget, headers: List[str]):
    table.setRowCount(table.rowCount() - 1)
    table.setColumnCount(len(headers))
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
    table.setHorizontalHeaderLabels(headers)
    table.resizeColumnsToContents()
    table.resizeRowsToContents()


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


class App:
    def __init__(self):
        self.thread = None
        self.window = uic.loadUi(resource_path("ui/mainwindow.ui"))
        self.window.button_send.clicked.connect(self.button_send_clicked)
        self.window.button_receive.clicked.connect(self.button_receive_clicked)
        self.window.button_pay.clicked.connect(self.button_pay_clicked)
        self.window.button_invoice.clicked.connect(self.button_invoice_clicked)
        self.window.show()
        self.async_warpper(self.init_wallet)
        self.set_app_icon()
        self.init_mainwindow()
        app.exec()
        return

    async def init_wallet(self):
        """Loads proofs from db."""
        await wallet.load_proofs()
        # await wallet.load_mint()
        self.load_mint_worker()
        self.mint_label_update_worker()
        self.update_main_label()

    def load_mint_worker(self):
        def load_mint_ready():
            self.update_main_label()

        self.mint_worker = worker.LoadMintWorker(wallet)
        self.load_mint_thread = QThread()
        self.mint_worker.finished.connect(load_mint_ready)
        self.mint_worker.moveToThread(self.load_mint_thread)
        self.load_mint_thread.started.connect(self.mint_worker.procLoadMint)
        self.load_mint_thread.start()

    def mint_label_update_worker(self):
        """Updates the mint label every n seconds"""

        def update_wallet_state():
            self.update_main_label()

        self.wallet_state_worker = worker.UpdateWalletStateWorker(wallet)
        self.wallet_state_thread = QThread()
        self.wallet_state_worker.update.connect(update_wallet_state)
        self.wallet_state_worker.moveToThread(self.wallet_state_thread)
        self.wallet_state_thread.started.connect(
            self.wallet_state_worker.procCheckWalletState
        )
        self.wallet_state_thread.start()

    def check_invoice_worker(self, invoice: Invoice):
        # https://stackoverflow.com/questions/6783194/background-thread-with-qthread-in-pyqt
        def invoice_worker_ready(status):
            print(f"got data from worker: {status}")
            if status == "paid":
                self.window.text_field.setPlainText("Payment received.")
                self.init_mainwindow()

        self.invoice_worker = worker.CheckInvoiceWorker(
            wallet.mint, invoice
        )
        if self.thread:
            self.thread.terminate()

        self.thread = QThread()
        self.invoice_worker.strReady.connect(invoice_worker_ready)
        self.invoice_worker.moveToThread(self.thread)
        self.invoice_worker.finished.connect(self.thread.quit)
        self.thread.started.connect(self.invoice_worker.procCounter)
        self.thread.start()

    def set_app_icon(self):
        # set app icon
        app_icon = QtGui.QIcon()
        app_icon.addFile("ui/icons/16x16.png", QtCore.QSize(16, 16))
        app_icon.addFile("ui/icons/24x24.png", QtCore.QSize(24, 24))
        app_icon.addFile("ui/icons/32x32.png", QtCore.QSize(32, 32))
        app_icon.addFile("ui/icons/48x48.png", QtCore.QSize(48, 48))
        app_icon.addFile("ui/icons/256x256.png", QtCore.QSize(256, 256))
        app_icon.addFile("ui/icons/512x512.png", QtCore.QSize(512, 512))
        app.setWindowIcon(app_icon)

    def update_main_label(self):
        label_text = f"Cashu {settings.version}"
        if settings.tor:
            running = False
            if hasattr(wallet, "tor"):
                running = wallet.tor.is_running()
            label_text += f" Tor: {'🟢' if running else '🔴'}"
        label_text += "\n"
        label_text += f"Mint: {wallet.url}"
        self.window.label_mint_url.setText(label_text)

    def init_mainwindow(self):
        self.window.tabWidget.setTabText(0, "Tokens")
        self.window.tabWidget.setTabText(1, "Pending")
        self.update_main_label()
        self.update_balance()
        self.list_amounts()
        self.list_pending()
        self.list_invoices()

    def update_balance(self):
        self.window.label_balance.setText(f"{wallet.available_balance} sat")

    def list_pending(self):
        table: QTableWidget = self.window.table_pending
        table.setRowCount(1)

        async def run(*args, **kwargs):
            reserved_proofs = await get_reserved_proofs(wallet.db)
            if len(reserved_proofs):
                sorted_proofs = sorted(reserved_proofs, key=itemgetter("send_id"))
                for i, (key, value) in enumerate(
                    groupby(sorted_proofs, key=itemgetter("send_id"))
                ):
                    grouped_proofs = list(value)
                    token = await wallet.serialize_proofs(grouped_proofs)
                    reserved_date = datetime.utcfromtimestamp(
                        int(grouped_proofs[0].time_reserved)
                    ).strftime("%Y-%m-%d %H:%M:%S")

                    rowPosition = table.rowCount() - 1
                    table.insertRow(rowPosition)
                    table.setItem(
                        rowPosition,
                        0,
                        QtWidgets.QTableWidgetItem(str(sum_proofs(grouped_proofs))),
                    )
                    table.setItem(rowPosition, 1, QtWidgets.QTableWidgetItem(str(token)))
                    table.setItem(
                        rowPosition,
                        2,
                        QtWidgets.QTableWidgetItem(str(reserved_date) or "none"),
                    )
                    table.setItem(rowPosition, 3, QtWidgets.QTableWidgetItem(str(key)))
            table_headers(table, ["amount", "token", "date", "id"])

        self.async_warpper(run)

    def list_amounts(self):

        table: QTableWidget = self.window.table_tokens
        table.setRowCount(1)
        sorted_proofs = sorted(wallet.proofs, key=lambda p: p.amount)
        for i, (key, value) in enumerate(groupby(sorted_proofs, lambda p: p.amount)):
            grouped_proofs = list(value)
            n_tokens = len(grouped_proofs)
            if n_tokens < 1:
                continue

            rowPosition = table.rowCount() - 1
            table.insertRow(rowPosition)
            table.setItem(rowPosition, 0, QtWidgets.QTableWidgetItem(str(key)))
            table.setItem(rowPosition, 1, QtWidgets.QTableWidgetItem(str(n_tokens)))
            table.setItem(
                rowPosition,
                2,
                QtWidgets.QTableWidgetItem(
                    str(sum([p.reserved or 0 for p in grouped_proofs]))
                )
                or "0",
            )
            table.setItem(
                rowPosition,
                3,
                QtWidgets.QTableWidgetItem(
                    str(" ".join(set([p.id for p in grouped_proofs])))
                ),
            )
        table_headers(table, ["amount", "count", "reserved", "keyset"])

    def list_invoices(self):
        table: QTableWidget = self.window.table_invoices
        table.setRowCount(1)

        async def run(*args, **kwargs):
            invoices: List[Invoice] = await get_lightning_invoices(db=wallet.db)
            for invoice in invoices:
                rowPosition = table.rowCount() - 1
                table.insertRow(rowPosition)
                table.setItem(
                    rowPosition, 0, QtWidgets.QTableWidgetItem(str(invoice.amount))
                )
                table.setItem(
                    rowPosition,
                    1,
                    QtWidgets.QTableWidgetItem(
                        str(f"{'paid' if invoice.paid else 'pending'}")
                    ),
                )
                table.setItem(
                    rowPosition, 2, QtWidgets.QTableWidgetItem(str(invoice.bolt11))
                )
            table_headers(table, ["amount", "status", "invoice"])
            table.cellDoubleClicked.connect(self.invoice_pending_clicked)

        self.async_warpper(run)

    def button_send_clicked(self, *args, **kwargs):
        async def run(*args, **kwargs):
            input = self.window.text_field.toPlainText()
            try:
                amount = int(input)
            except:
                print("no numeric amount in input field.")
                return
            if not amount > 0:
                print("amount must be greater than 0.")
                return

            _, send_proofs = await wallet.split_to_send(
                wallet.proofs, amount, set_reserved=True
            )

            token = await wallet.serialize_proofs(send_proofs, include_mints=True)
            await wallet.set_reserved(send_proofs, reserved=True)
            print(f"Send Clicked! {token}")
            self.window.text_field.setPlainText(token)

        self.async_warpper(run)
        self.init_mainwindow()

    def button_receive_clicked(self, *args, **kwargs):
        async def run(*args, **kwargs):
            input = self.window.text_field.toPlainText()
            if len(input) < 16:
                raise Exception("no proof provided.")
            token = input
            tokenObj = deserialize_token_from_string(token)
            # TODO: verify that we trust all mints in these tokens
            # ask the user if they want to trust the new mints
            #for mint_url in set([t.mint for t in tokenObj.token if t.mint]):
            #    mint_wallet = Wallet(
            #        mint_url, os.path.join(settings.cashu_dir, wallet.name)
            #    )
            #    await verify_mint(mint_wallet, mint_url)
            await receive(wallet, tokenObj)
            print(f"Receive Clicked! {token}")
            self.window.text_field.setPlainText("")

        self.async_warpper(run)
        self.init_mainwindow()

    def button_pay_clicked(self, *args, **kwargs):
        print("Pay Clicked!")

        async def run(*args, **kwargs):
            input = self.window.text_field.toPlainText()
            if len(input) < 16 or not input.startswith("lnbc"):
                raise Exception("invalid Lightning invoice.")
            invoice = input
            proofs = await wallet.split_to_pay(invoice)
            await wallet.pay_lightning(proofs, invoice)
            self.window.text_field.setPlainText("Invoice paid.")

        self.async_warpper(run)
        self.init_mainwindow()

    def button_invoice_clicked(self, *args, **kwargs):
        print("Invoice Clicked!")

        async def run(*args, **kwargs):
            input = self.window.text_field.toPlainText()
            try:
                amount = int(input)
            except:
                print("no numeric amount in input field.")
                return
            if not amount > 0:
                print("amount must be greater than 0.")
                return
            invoice = await wallet.request_mint(amount)
            if invoice.bolt11:
                self.window.text_field.setPlainText(invoice.bolt11)
                # kick off the worker that checks this invoice
                try:
                    self.check_invoice_worker(invoice)
                except:
                    traceback.print_exception(e)
                    pass
                return amount, invoice

        amount, invoice = self.async_warpper(run)
        self.init_mainwindow()

    def invoice_pending_clicked(self, *args, **kwargs):
        """Activates when the user presses the "pending" cell in the invoice table."""

        async def run(*args, **kwargs):
            print(f"clicked {args}")
            if args[1] == 1:
                # pending column clicked
                invoices: List[Invoice] = await get_lightning_invoices(db=wallet.db)
                # get correct invoice
                invoice = invoices[args[0]]
                try:
                    await wallet.mint(invoice.amount, invoice.hash)
                    paid = True
                    self.window.text_field.setPlainText("Invoice paid.")
                except Exception as e:
                    traceback.print_exception(e)
                    self.show_error(str(e))
                    pass

        self.async_warpper(run, *args, **kwargs)
        self.init_mainwindow()

    async def printer(self):
        while True:
            time.sleep(1)
            print("printer")

    def async_warpper(self, f, *args, **kwargs):
        try:
            return asyncio.run(f(*args, **kwargs))
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            self.show_error(str(e))
            return e

    def show_error(self, msg):
        dlg = QtWidgets.QMessageBox.critical(
            self.window,
            "Error",
            f"Error: {msg}",
            buttons=QtWidgets.QMessageBox.StandardButton.Ok,
        )


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Cashu")
    app = App()
